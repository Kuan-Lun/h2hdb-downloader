import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import DownloadRequest
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from h2hdb_downloader.downloader import TagCascadePolicy

if TYPE_CHECKING:
    from h2hdb_downloader.downloader import Downloader

    from .conftest import FakeDBStore, FakeDriver


def gallery(gid: int) -> GalleryURLParser:
    return GalleryURLParser(url=f"https://exhentai.org/g/{gid}/deadbeef00/")


def gids_of(galleries: list[GalleryURLParser]) -> list[int]:
    return [gallery.gid for gallery in galleries]


async def test_download_requests_work_before_network_and_completes_on_success(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1}
    fake_store.pending_download_gids = [1]

    async def assert_request_exists(target: GalleryURLParser) -> bool:
        request = fake_store.download_requests[target.gid]
        assert request.url == target.url
        return True

    fake_driver.download_result = assert_request_exists
    downloader = downloader_factory()

    result = await downloader.download_by_gallery(gallery(1))

    assert result == {1: True}
    assert fake_store.redownload_time_updates == [1]
    assert fake_store.download_requests == {}


async def test_download_false_keeps_durable_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_driver.download_result = False
    downloader = downloader_factory()

    assert await downloader.download_by_gallery(gallery(1)) == {1: False}

    request = fake_store.download_requests[1]
    assert request.url == gallery(1).url
    assert downloader._queue.wocount == 0


async def test_newer_request_created_during_download_survives_old_completion(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    async def replace_request(target: GalleryURLParser) -> bool:
        old_request = fake_store.download_requests[target.gid]
        fake_store.download_requests[target.gid] = DownloadRequest(
            target.gid,
            target.url,
            "newer-token",
        )
        assert old_request.token != "newer-token"
        return True

    fake_driver.download_result = replace_request
    downloader = downloader_factory()

    assert await downloader.download_by_gallery(gallery(1)) == {1: True}
    assert fake_store.download_requests[1].token == "newer-token"


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
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    attempts = {"count": 0}
    attempted_tokens = list[str]()

    async def flaky(target: GalleryURLParser) -> bool:
        attempts["count"] += 1
        attempted_tokens.append(fake_store.download_requests[target.gid].token)
        if attempts["count"] == 1:
            raise ClientOfflineException("offline")
        return True

    fake_driver.download_result = flaky
    downloader = downloader_factory(wait4client=30)

    result = await downloader.download_by_gallery(gallery(1))

    assert result == {1: True}
    assert attempts["count"] == 2
    assert len(set(attempted_tokens)) == 1
    assert fake_store.download_requests == {}


async def test_retry_does_not_overwrite_request_created_while_waiting(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    replacement = DownloadRequest(1, gallery(1).url, "external-token")

    async def fail_after_external_request(target: GalleryURLParser) -> bool:
        fake_store.download_requests[target.gid] = replacement
        raise ClientOfflineException("offline")

    fake_driver.download_result = fail_after_external_request
    downloader = downloader_factory(wait4client=30)

    result = await downloader.download_by_gallery(gallery(1))

    assert result == {1: False}
    assert gids_of(fake_driver.download_calls) == [1]
    assert fake_store.download_requests == {1: replacement}


async def test_insufficient_funds_with_zero_retry_window_reraises(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    async def always_fails(_gallery: GalleryURLParser) -> bool:
        raise InsufficientFundsException("broke")

    fake_driver.download_result = always_fails
    downloader = downloader_factory(retry2download=0)

    with pytest.raises(InsufficientFundsException):
        await downloader.download_by_gallery(gallery(1))

    assert fake_store.download_requests[1].url == gallery(1).url


async def test_cancelled_download_keeps_durable_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    async def cancel(_gallery: GalleryURLParser) -> bool:
        raise asyncio.CancelledError

    fake_driver.download_result = cancel
    downloader = downloader_factory()

    with pytest.raises(asyncio.CancelledError):
        await downloader.download_by_gallery(gallery(1))

    assert fake_store.download_requests[1].url == gallery(1).url


async def test_download_by_gid_marks_removed_when_gallery_no_longer_exists(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    def assert_request_precedes_search(key: str, _isclear: bool) -> None:
        gid = int(key.removeprefix("gid:"))
        assert fake_store.download_requests[gid].gid == gid

    fake_driver.search_observer = assert_request_precedes_search
    downloader = downloader_factory()

    result = await downloader.download_by_gid(404)

    assert result == {}
    assert 404 in fake_store.removed_gids
    assert fake_store.download_requests == {}


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
    # Simulate a prior run that left gid 1 requested, while gid 2 is flagged
    # by the DB as needing a periodic redownload.
    fake_store.gids = {1, 2}
    fake_store.download_requests = {1: DownloadRequest(1, gallery(1).url, "request-1")}
    fake_store.pending_download_gids = [2]
    fake_driver.search_results["gid:2"] = [gallery(2)]

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    await downloader.drain_queue(policy, skip_check=True)
    for gid in downloader.pending_redownload_gids():
        await downloader.deep_download_by_gid(gid, policy, skip_check=True)

    assert set(gids_of(fake_driver.download_calls)) == {1, 2}
    assert downloader.pending_redownload_gids() == []
    assert fake_store.download_requests == {}


async def test_drain_queue_url_entry_falls_back_to_gid_search_on_failure(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    """A queued URL never falls back to a gid search by itself, so if the
    direct download fails, drain_queue must retry via gid to let h2hdb learn
    the gallery is gone (rather than silently dropping the queue entry)."""
    fake_store.download_requests = {1: DownloadRequest(1, gallery(1).url, "request-1")}
    fake_driver.download_result = False
    fake_driver.search_results["gid:1"] = []  # gallery no longer exists

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    await downloader.drain_queue(policy, skip_check=True)

    assert 1 in fake_store.removed_gids
    assert fake_store.download_requests == {}


async def test_drain_queue_url_entry_skips_fallback_when_direct_download_succeeds(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(1, gallery(1).url, "request-1")
    fake_store.download_requests = {1: request}

    async def assert_same_request(target: GalleryURLParser) -> bool:
        assert fake_store.download_requests[target.gid] == request
        return True

    fake_driver.download_result = assert_same_request

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    result = await downloader.drain_queue(policy, skip_check=True)

    assert result == {1: True}
    assert fake_driver.search_calls == []
    assert fake_store.download_requests == {}


async def test_drain_queue_false_result_keeps_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(1, gallery(1).url, "request-1")
    fake_store.download_requests = {1: request}
    fake_driver.download_result = False
    fake_driver.search_results["gid:1"] = [gallery(1)]
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {1: False}
    assert fake_store.download_requests == {1: request}


async def test_drain_queue_exception_keeps_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(1, gallery(1).url, "request-1")
    fake_store.download_requests = {1: request}

    async def fail(_gallery: GalleryURLParser) -> bool:
        raise InsufficientFundsException("broke")

    fake_driver.download_result = fail
    downloader = downloader_factory(retry2download=0)

    with pytest.raises(InsufficientFundsException):
        await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert fake_store.download_requests == {1: request}


async def test_drain_queue_removed_gid_completes_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
) -> None:
    request = DownloadRequest(404, "", "request-404")
    fake_store.download_requests = {404: request}
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {}
    assert 404 in fake_store.removed_gids
    assert fake_store.download_requests == {}


async def test_drain_queue_redirect_success_completes_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(999, "", "request-999")
    fake_store.download_requests = {999: request}
    fake_store.gids = {999}
    fake_driver.search_results["gid:999"] = [gallery(1)]
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {1: True}
    assert 999 in fake_store.todelete_gids
    assert fake_store.download_requests == {}


async def test_drain_queue_redirect_failure_keeps_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(999, "", "request-999")
    fake_store.download_requests = {999: request}
    fake_store.gids = {999}
    fake_driver.search_results["gid:999"] = [gallery(1)]
    fake_driver.download_result = False
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {1: False}
    assert 999 not in fake_store.todelete_gids
    assert fake_store.download_requests == {999: request}
