from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from h2hdb_downloader.downloader import TagCascadePolicy

if TYPE_CHECKING:
    from h2hdb_downloader.downloader import Downloader

    from .conftest import FakeDBStore, FakeDriver


def gallery(gid: int) -> GalleryURLParser:
    return GalleryURLParser(url=f"https://exhentai.org/g/{gid}/deadbeef00/")


def gids_of(galleries: list[GalleryURLParser]) -> list[int]:
    return [gallery.gid for gallery in galleries]


async def test_download_marks_done_and_updates_redownload_time(
    downloader_factory: Callable[..., Downloader], fake_store: FakeDBStore
) -> None:
    fake_store.gids = {1}
    fake_store.pending_download_gids = [1]
    downloader = downloader_factory()

    result = await downloader.download_by_gallery(gallery(1))

    assert result == {1: True}
    assert fake_store.redownload_time_updates == [1]
    assert 1 in downloader._queue.pass_gids
    assert fake_store.todownload == {}


async def test_download_skips_already_settled_gid_without_hitting_driver(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1}
    downloader = downloader_factory()
    downloader._queue.wocount_max = 1000  # never force a re-verify

    result = await downloader.download_by_gallery(gallery(1))

    assert fake_driver.download_calls == []
    assert result == {1: False}


async def test_wocount_overflow_forces_reverify_even_when_settled(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1}
    downloader = downloader_factory()
    downloader._queue.wocount = downloader._queue.wocount_max + 1

    await downloader.download_by_gallery(gallery(1))

    assert gids_of(fake_driver.download_calls) == [1]


async def test_client_offline_retries_and_eventually_succeeds(
    downloader_factory: Callable[..., Downloader], fake_driver: FakeDriver
) -> None:
    attempts = {"count": 0}

    async def flaky(_gallery: GalleryURLParser) -> bool:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ClientOfflineException("offline")
        return True

    fake_driver.download_result = flaky
    downloader = downloader_factory(wait4client=30)

    result = await downloader.download_by_gallery(gallery(1))

    assert result == {1: True}
    assert attempts["count"] == 2


async def test_insufficient_funds_with_zero_retry_window_reraises(
    downloader_factory: Callable[..., Downloader], fake_driver: FakeDriver
) -> None:
    async def always_fails(_gallery: GalleryURLParser) -> bool:
        raise InsufficientFundsException("broke")

    fake_driver.download_result = always_fails
    downloader = downloader_factory(retry2download=0)

    with pytest.raises(InsufficientFundsException):
        await downloader.download_by_gallery(gallery(1))


async def test_download_by_gid_marks_removed_when_gallery_no_longer_exists(
    downloader_factory: Callable[..., Downloader], fake_store: FakeDBStore
) -> None:
    downloader = downloader_factory()

    result = await downloader.download_by_gid(404)

    assert result == {}
    assert 404 in fake_store.removed_gids


async def test_download_by_gid_marks_todelete_when_gid_redirects(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {999}
    fake_driver.search_results["gid:999"] = [gallery(1)]
    downloader = downloader_factory()

    result = await downloader.download_by_gid(999)

    assert result == {1: True}
    assert 999 in fake_store.todelete_gids


async def test_deep_download_cascades_into_tags(
    downloader_factory: Callable[..., Downloader], fake_driver: FakeDriver
) -> None:
    seed = gallery(1)
    sibling = gallery(2)
    tag = SimpleNamespace(href="https://exhentai.org/tag/artist:someone")
    fake_driver.tag_results["artist"] = [tag]
    fake_driver.search_results[""] = [sibling]

    downloader = downloader_factory()
    result = await downloader.deep_download_by_gallery(
        seed, TagCascadePolicy(filters=("artist",), conditions=()), skip_check=False
    )

    assert result == {1: True, 2: True}
    assert fake_driver.get_calls == [tag.href]


async def test_deep_download_skips_cascade_when_seed_skipped_and_no_skip_check(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1}
    downloader = downloader_factory()
    downloader._queue.wocount_max = 1000

    result = await downloader.deep_download_by_gallery(
        gallery(1),
        TagCascadePolicy(filters=("artist",), conditions=()),
        skip_check=False,
    )

    assert result == {1: False}
    assert fake_driver.gallery2tag_calls == []


async def test_deep_download_skip_check_forces_cascade_despite_seed_being_skipped(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    seed = gallery(1)
    sibling = gallery(2)
    tag = SimpleNamespace(href="https://exhentai.org/tag/artist:someone")
    fake_store.gids = {1}
    fake_driver.tag_results["artist"] = [tag]
    fake_driver.search_results[""] = [sibling]

    downloader = downloader_factory()
    downloader._queue.wocount_max = 1000

    result = await downloader.deep_download_by_gallery(
        seed, TagCascadePolicy(filters=("artist",), conditions=()), skip_check=True
    )

    assert result == {1: False, 2: True}


async def test_download_by_gid_settles_pending_gid_even_when_removed(
    downloader_factory: Callable[..., Downloader], fake_store: FakeDBStore
) -> None:
    fake_store.gids = {404}
    fake_store.pending_download_gids = [404]
    downloader = downloader_factory()

    await downloader.download_by_gid(404)

    assert downloader.pending_redownload_gids() == []


async def test_download_by_gid_settles_original_gid_when_redirected(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {999}
    fake_store.pending_download_gids = [999]
    fake_driver.search_results["gid:999"] = [gallery(1)]
    downloader = downloader_factory()

    await downloader.download_by_gid(999)

    assert downloader.pending_redownload_gids() == []


async def test_pending_redownload_gids_returns_a_snapshot_copy(
    downloader_factory: Callable[..., Downloader], fake_store: FakeDBStore
) -> None:
    fake_store.gids = {1}
    fake_store.pending_download_gids = [1]
    downloader = downloader_factory()

    snapshot = downloader.pending_redownload_gids()
    snapshot.append(999)

    assert downloader.pending_redownload_gids() == [1]


async def test_application_loop_drains_residual_queue_then_redownloads_pending(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    """Mirrors the loop example-main.py writes against the public API:
    drain_queue() once, then settle every pending gid via deep_download_by_gid(),
    with no private state touched."""
    # Simulate a prior run that crashed mid-download: gid 1 is left in the
    # in-flight log, and gid 2 is flagged by the DB as needing a redownload.
    fake_store.gids = {1, 2}
    fake_store.todownload = {1: gallery(1).url}
    fake_store.pending_download_gids = [2]
    fake_driver.search_results["gid:2"] = [gallery(2)]

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    await downloader.drain_queue(policy, skip_check=True)
    for gid in downloader.pending_redownload_gids():
        await downloader.deep_download_by_gid(gid, policy, skip_check=True)

    assert set(gids_of(fake_driver.download_calls)) == {1, 2}
    assert downloader.pending_redownload_gids() == []
    assert fake_store.todownload == {}


async def test_drain_queue_url_entry_falls_back_to_gid_search_on_failure(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    """A queued URL never falls back to a gid search by itself, so if the
    direct download fails, drain_queue must retry via gid to let h2hdb learn
    the gallery is gone (rather than silently dropping the queue entry)."""
    fake_store.todownload = {1: gallery(1).url}
    fake_driver.download_result = False
    fake_driver.search_results["gid:1"] = []  # gallery no longer exists

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    await downloader.drain_queue(policy, skip_check=True)

    assert 1 in fake_store.removed_gids
    assert fake_store.todownload == {}


async def test_drain_queue_url_entry_skips_fallback_when_direct_download_succeeds(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.todownload = {1: gallery(1).url}

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    result = await downloader.drain_queue(policy, skip_check=True)

    assert result == {1: True}
    assert fake_driver.search_calls == []
