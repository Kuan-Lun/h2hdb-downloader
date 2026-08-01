import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import DownloadRequest
from hbrowser import (
    ConfirmedGalleryMissing,
    GalleryFound,
    MalformedSearchPageError,
    SearchRequest,
    Tag,
)
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from h2hdb_downloader import DownloadTurnLostError
from h2hdb_downloader.downloader import TagCascadePolicy

if TYPE_CHECKING:
    from h2hdb_downloader.downloader import Downloader

    from .conftest import FakeDBStore, FakeDriver


def gallery(gid: int) -> GalleryURLParser:
    return GalleryURLParser(url=f"https://exhentai.org/g/{gid}/deadbeef00/")


def gids_of(galleries: list[GalleryURLParser]) -> list[int]:
    return [gallery.gid for gallery in galleries]


async def wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


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
    def assert_request_precedes_lookup(gid: int) -> None:
        assert fake_store.download_requests[gid].gid == gid

    fake_driver.lookup_observer = assert_request_precedes_lookup
    fake_driver.lookup_results[404] = ConfirmedGalleryMissing(404, 2)
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
    fake_store.removed_gids = {1, 999}
    fake_driver.lookup_results[999] = GalleryFound(999, gallery(1))
    downloader = downloader_factory()

    result = await downloader.download_by_gid(999)

    assert result == {1: True}
    assert 999 in fake_store.todelete_gids
    assert fake_store.removed_gids == set()


async def test_found_gid_clears_stale_missing_marker_even_when_download_fails(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.removed_gids = {1}
    fake_driver.lookup_results[1] = GalleryFound(1, gallery(1))
    fake_driver.download_result = False
    downloader = downloader_factory()

    assert await downloader.download_by_gid(1) == {1: False}

    assert fake_store.removed_gids == set()
    assert 1 in fake_store.download_requests


async def test_download_by_gid_rejects_mismatched_lookup_result(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_driver.lookup_results[1] = ConfirmedGalleryMissing(2, 2)
    downloader = downloader_factory()

    with pytest.raises(RuntimeError, match="wrong GID"):
        await downloader.download_by_gid(1)

    assert 1 in fake_store.download_requests
    assert fake_store.removed_gids == set()


async def test_deep_download_cascades_into_tags(
    downloader_factory: Callable[..., Downloader], fake_driver: FakeDriver
) -> None:
    seed = gallery(1)
    sibling = gallery(2)
    tag = SimpleNamespace(href="https://exhentai.org/tag/artist:someone")
    fake_driver.tag_results["artist"] = [cast(Tag, tag)]
    fake_driver.search_results[(tag.href, "")] = (sibling,)

    downloader = downloader_factory()
    result = await downloader.deep_download_by_gallery(
        seed, TagCascadePolicy(filters=("artist",), conditions=()), skip_check=False
    )

    assert result == {1: True, 2: True}
    assert fake_driver.search_calls == [SearchRequest(scope_url=tag.href, query="")]


async def test_download_by_tag_uses_an_explicit_request_for_each_condition(
    downloader_factory: Callable[..., Downloader],
    fake_driver: FakeDriver,
) -> None:
    tag = cast(
        Tag,
        SimpleNamespace(href="https://exhentai.org/tag/artist:someone"),
    )
    conditions = ("language:chinese$", "language:speechless$")
    fake_driver.search_results[(tag.href, conditions[0])] = (gallery(1),)
    fake_driver.search_results[(tag.href, conditions[1])] = (gallery(2),)
    downloader = downloader_factory()

    result = await downloader.download_by_tag(tag, conditions)

    assert result == {1: True, 2: True}
    assert fake_driver.search_calls == [
        SearchRequest(scope_url=tag.href, query=condition) for condition in conditions
    ]


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
    fake_driver.tag_results["artist"] = [cast(Tag, tag)]
    fake_driver.search_results[(tag.href, "")] = (sibling,)

    downloader = downloader_factory()
    downloader._queue.wocount_max = 1000

    result = await downloader.deep_download_by_gallery(
        seed, TagCascadePolicy(filters=("artist",), conditions=()), skip_check=True
    )

    assert result == {1: False, 2: True}


async def test_download_by_gid_settles_pending_gid_even_when_removed(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {404}
    fake_store.pending_download_gids = [404]
    fake_driver.lookup_results[404] = ConfirmedGalleryMissing(404, 2)
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
    fake_driver.lookup_results[999] = GalleryFound(999, gallery(1))
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
    """The two durable snapshots can be drained without per-gid ingest turns."""
    # Simulate a prior run that left gid 1 requested, while gid 2 is flagged
    # by the DB as needing a periodic redownload.
    fake_store.gids = {1, 2}
    fake_store.download_requests = {1: DownloadRequest(1, gallery(1).url, "request-1")}
    fake_store.pending_download_gids = [2]
    fake_driver.lookup_results[2] = GalleryFound(2, gallery(2))

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    await downloader.drain_queue(policy, skip_check=True)
    await downloader.drain_pending_redownloads(policy, skip_check=True)

    assert set(gids_of(fake_driver.download_calls)) == {1, 2}
    assert downloader.pending_redownload_gids() == []
    assert fake_store.download_requests == {}


async def test_pending_redownloads_use_submission_threshold_batches(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1, 2, 3}
    fake_store.pending_download_gids = [1, 2, 3]
    fake_driver.lookup_results = {
        gid: GalleryFound(gid, gallery(gid)) for gid in (1, 2, 3)
    }

    def assert_snapshot_was_preseeded(gid: int) -> None:
        if gid == 1:
            assert set(fake_store.download_requests) == {1, 2, 3}

    fake_driver.lookup_observer = assert_snapshot_was_preseeded
    downloader = downloader_factory(download_submissions_per_ingest=2)

    result = await downloader.drain_pending_redownloads(
        TagCascadePolicy(filters=(), conditions=())
    )

    assert result == {1: True, 2: True, 3: True}
    assert downloader.pending_redownload_gids() == []
    assert fake_store.download_requests == {}
    assert [
        turn.generation
        for turn, _request in fake_store.completed_download_requests_in_turn
    ] == [1, 1, 2]
    assert [turn.generation for turn in fake_store.gallery_ingest_requests] == [
        1,
        2,
    ]


async def test_pending_related_root_reuses_preseeded_token_and_runs_its_cascade(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1, 2}
    fake_store.pending_download_gids = [1, 2]
    fake_driver.lookup_results = {
        gid: GalleryFound(gid, gallery(gid)) for gid in (1, 2)
    }
    root_one_tag = cast(
        Tag,
        SimpleNamespace(href="https://exhentai.org/tag/artist:root-one"),
    )
    root_two_tag = cast(
        Tag,
        SimpleNamespace(href="https://exhentai.org/tag/artist:root-two"),
    )
    fake_driver.gallery_tag_results[(1, "artist")] = [root_one_tag]
    fake_driver.gallery_tag_results[(2, "artist")] = [root_two_tag]
    fake_driver.search_results[(root_one_tag.href, "")] = (gallery(2), gallery(3))
    fake_driver.search_results[(root_two_tag.href, "")] = (gallery(4),)
    root_two_token: str | None = None

    def observe_search(request: SearchRequest) -> None:
        nonlocal root_two_token
        if request.scope_url == root_one_tag.href:
            root_two_token = fake_store.download_requests[2].token
        else:
            assert request.scope_url == root_two_tag.href
            assert root_two_token is not None
            assert fake_store.download_requests[2].token == root_two_token

    fake_driver.search_observer = observe_search
    downloader = downloader_factory(download_submissions_per_ingest=100)

    result = await downloader.drain_pending_redownloads(
        TagCascadePolicy(filters=("artist",), conditions=()),
        skip_check=False,
    )

    assert result == {1: True, 2: True, 3: True, 4: True}
    assert gids_of(fake_driver.download_calls) == [1, 2, 3, 4]
    assert fake_store.download_requests == {}
    assert [
        (turn.generation, request.gid)
        for turn, request in fake_store.completed_download_requests_in_turn
    ] == [(1, 1), (1, 2)]
    assert len(fake_store.gallery_ingest_requests) == 1


async def test_interrupted_pending_snapshot_preseeding_leaves_recoverable_requests(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1, 2, 3}
    fake_store.pending_download_gids = [1, 2, 3]
    ensure_calls = 0

    def interrupt_third_ensure() -> None:
        nonlocal ensure_calls
        ensure_calls += 1
        if ensure_calls == 3:
            raise RuntimeError("simulated process interruption")

    fake_store.request_download_observer = interrupt_third_ensure
    downloader = downloader_factory()

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        await downloader.drain_pending_redownloads(
            TagCascadePolicy(filters=(), conditions=())
        )

    assert set(fake_store.download_requests) == {1, 2}
    assert fake_driver.download_calls == []
    assert fake_store.claim_download_turn_calls == []
    assert downloader.pending_redownload_gids() == [1, 2, 3]


async def test_pending_batch_exception_preserves_current_and_later_requests(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1, 2, 3}
    fake_store.pending_download_gids = [1, 2, 3]
    fake_driver.lookup_results = {
        gid: GalleryFound(gid, gallery(gid)) for gid in (1, 2, 3)
    }

    async def fail_second(target: GalleryURLParser) -> bool:
        if target.gid == 2:
            raise InsufficientFundsException("broke")
        return True

    fake_driver.download_result = fail_second
    downloader = downloader_factory(retry2download=0)

    with pytest.raises(InsufficientFundsException):
        await downloader.drain_pending_redownloads(
            TagCascadePolicy(filters=(), conditions=())
        )

    assert set(fake_store.download_requests) == {2, 3}
    assert [
        request.gid for _turn, request in fake_store.completed_download_requests_in_turn
    ] == [1]
    assert gids_of(fake_driver.download_calls) == [1, 2]
    assert len(fake_store.gallery_ingest_requests) == 1


async def test_pending_batch_cancellation_preserves_current_and_later_requests(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1, 2, 3}
    fake_store.pending_download_gids = [1, 2, 3]
    fake_driver.lookup_results = {
        gid: GalleryFound(gid, gallery(gid)) for gid in (1, 2, 3)
    }
    second_started = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def block_second(target: GalleryURLParser) -> bool:
        if target.gid == 2:
            second_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                second_cancelled.set()
        return True

    fake_driver.download_result = block_second
    downloader = downloader_factory()
    task = asyncio.create_task(
        downloader.drain_pending_redownloads(
            TagCascadePolicy(filters=(), conditions=())
        )
    )
    await second_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert second_cancelled.is_set()
    assert set(fake_store.download_requests) == {2, 3}
    assert [
        request.gid for _turn, request in fake_store.completed_download_requests_in_turn
    ] == [1]
    assert len(fake_store.gallery_ingest_requests) == 1


async def test_cancelling_pending_boundary_wait_leaves_later_preseeded_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.gids = {1, 2}
    fake_store.pending_download_gids = [1, 2]
    fake_store.auto_complete_gallery_ingest = False
    fake_driver.lookup_results = {
        gid: GalleryFound(gid, gallery(gid)) for gid in (1, 2)
    }
    downloader = downloader_factory(download_submissions_per_ingest=1)
    task = asyncio.create_task(
        downloader.drain_pending_redownloads(
            TagCascadePolicy(filters=(), conditions=())
        )
    )

    await wait_until(lambda: len(fake_store.gallery_ingest_requests) == 1)
    assert gids_of(fake_driver.download_calls) == [1]
    assert set(fake_store.download_requests) == {2}
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert set(fake_store.download_requests) == {2}
    assert fake_store.completed_ingest_generation == 0


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
    fake_driver.lookup_results[1] = ConfirmedGalleryMissing(1, 2)

    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    await downloader.drain_queue(policy, skip_check=True)

    assert 1 in fake_store.removed_gids
    assert fake_store.download_requests == {}
    assert len(fake_store.claim_download_turn_calls) == 1
    assert len(fake_store.completed_missing_download_requests_in_turn) == 1
    assert fake_store.finished_download_turns == []
    assert len(fake_store.gallery_ingest_requests) == 1


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
    fake_driver.lookup_results[1] = GalleryFound(1, gallery(1))
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


async def test_drain_queue_search_error_keeps_request_and_hands_off(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(349189, "", "request-349189")
    fake_store.download_requests = {request.gid: request}

    error = MalformedSearchPageError(
        query="gid:349189",
        url="https://exhentai.org/?f_search=gid%3A349189",
        title="Gallery List",
        reason="search page did not reach a terminal state",
    )

    def fail_lookup(gid: int) -> None:
        assert gid == 349189
        raise error

    fake_driver.lookup_observer = fail_lookup
    downloader = downloader_factory()

    with pytest.raises(MalformedSearchPageError) as raised:
        await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert raised.value is error
    assert fake_store.download_requests == {request.gid: request}
    assert fake_store.removed_gids == set()
    assert len(fake_store.gallery_ingest_requests) == 1
    assert fake_store.finished_download_turns == []


async def test_failed_root_reports_turn_loss_when_exception_handoff_is_rejected(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(349189, "", "request-349189")
    fake_store.download_requests = {request.gid: request}
    error = MalformedSearchPageError(
        query="gid:349189",
        url="https://exhentai.org/?f_search=gid%3A349189",
        title="Gallery List",
        reason="search page did not reach a terminal state",
    )

    def fail_after_losing_turn(gid: int) -> None:
        assert gid == request.gid
        fake_store.active_download_turn = None
        raise error

    fake_driver.lookup_observer = fail_after_losing_turn
    downloader = downloader_factory()

    with pytest.raises(DownloadTurnLostError) as raised:
        await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert raised.value.__cause__ is error
    assert fake_store.download_requests == {request.gid: request}
    assert fake_store.removed_gids == set()
    assert len(fake_store.gallery_ingest_requests) == 1


async def test_drain_queue_removed_gid_completes_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(404, "", "request-404")
    fake_store.download_requests = {404: request}
    fake_driver.lookup_results[404] = ConfirmedGalleryMissing(404, 2)
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {}
    assert 404 in fake_store.removed_gids
    assert fake_store.download_requests == {}
    assert len(fake_store.completed_missing_download_requests_in_turn) == 1
    assert fake_store.finished_download_turns == []


async def test_missing_lookup_cannot_settle_a_newer_request_token(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    stale_request = DownloadRequest(404, "", "request-404")
    newer_request = DownloadRequest(404, gallery(404).url, "newer-request-404")
    fake_store.download_requests = {404: stale_request}
    fake_driver.lookup_results[404] = ConfirmedGalleryMissing(404, 2)

    def replace_request_during_lookup(gid: int) -> None:
        assert gid == stale_request.gid
        fake_store.download_requests[gid] = newer_request

    fake_driver.lookup_observer = replace_request_during_lookup
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {}
    assert fake_store.download_requests == {404: newer_request}
    assert fake_store.removed_gids == set()
    assert len(fake_store.completed_missing_download_requests_in_turn) == 1
    assert fake_store.completed_ingest_generation == 1


async def test_drain_queue_redirect_success_completes_original_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    request = DownloadRequest(999, "", "request-999")
    fake_store.download_requests = {999: request}
    fake_store.gids = {999}
    fake_driver.lookup_results[999] = GalleryFound(999, gallery(1))
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
    fake_driver.lookup_results[999] = GalleryFound(999, gallery(1))
    fake_driver.download_result = False
    downloader = downloader_factory()

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {1: False}
    assert 999 not in fake_store.todelete_gids
    assert fake_store.download_requests == {999: request}


def test_turn_timing_configuration_is_validated(
    downloader_factory: Callable[..., Downloader],
) -> None:
    assert downloader_factory().download_submissions_per_ingest == 100
    with pytest.raises(ValueError, match="turn_poll_seconds"):
        downloader_factory(turn_poll_seconds=0)
    with pytest.raises(ValueError, match="turn_poll_seconds"):
        downloader_factory(turn_poll_seconds=float("nan"))
    with pytest.raises(ValueError, match="turn_lease_seconds"):
        downloader_factory(turn_lease_seconds=0)
    with pytest.raises(ValueError, match="turn_lease_seconds"):
        downloader_factory(turn_lease_seconds=300.5)
    with pytest.raises(ValueError, match="turn_heartbeat_seconds"):
        downloader_factory(turn_heartbeat_seconds=0)
    with pytest.raises(ValueError, match="shorter"):
        downloader_factory(
            turn_lease_seconds=60,
            turn_heartbeat_seconds=60,
        )
    for invalid_batch_size in (True, 0, -1, 1.5):
        with pytest.raises(ValueError, match="download_submissions_per_ingest"):
            downloader_factory(download_submissions_per_ingest=invalid_batch_size)


async def test_deep_root_waits_for_its_ingest_generation(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.auto_complete_gallery_ingest = False
    fake_driver.lookup_results[1] = GalleryFound(1, gallery(1))
    downloader = downloader_factory()

    task = asyncio.create_task(
        downloader.deep_download_by_gid(
            1,
            TagCascadePolicy(filters=(), conditions=()),
        )
    )
    await wait_until(lambda: fake_store.handed_off_turn is not None)

    assert not task.done()
    assert fake_store.download_requests == {}
    turn = fake_store.handed_off_turn
    assert turn is not None
    assert fake_store.completed_ingest_generation < turn.generation

    fake_store.complete_gallery_ingest()

    assert await task == {1: True}
    assert fake_store.completed_ingest_generation == turn.generation


async def test_deep_root_does_not_start_network_work_before_claiming_ready_turn(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.download_turn_available = False
    downloader = downloader_factory()
    task = asyncio.create_task(
        downloader.deep_download_by_gallery(
            gallery(1),
            TagCascadePolicy(filters=(), conditions=()),
        )
    )

    await wait_until(lambda: len(fake_store.claim_download_turn_calls) >= 1)
    assert fake_driver.download_calls == []
    assert fake_store.download_requests == {}

    fake_store.download_turn_available = True

    assert await task == {1: True}
    assert len(fake_store.claim_download_turn_calls) >= 2


async def test_deep_root_request_survives_until_related_cascade_finishes(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    seed = gallery(1)
    sibling = gallery(2)
    tag = SimpleNamespace(href="https://exhentai.org/tag/artist:someone")
    fake_driver.tag_results["artist"] = [cast(Tag, tag)]
    fake_driver.search_results[(tag.href, "")] = (sibling,)
    sibling_started = asyncio.Event()
    release_sibling = asyncio.Event()

    async def block_sibling(target: GalleryURLParser) -> bool:
        if target.gid == sibling.gid:
            sibling_started.set()
            await release_sibling.wait()
        return True

    fake_driver.download_result = block_sibling
    downloader = downloader_factory()
    task = asyncio.create_task(
        downloader.deep_download_by_gallery(
            seed,
            TagCascadePolicy(filters=("artist",), conditions=()),
        )
    )

    await sibling_started.wait()
    assert seed.gid in fake_store.download_requests

    release_sibling.set()

    assert await task == {1: True, 2: True}
    assert seed.gid not in fake_store.download_requests
    assert len(fake_store.claim_download_turn_calls) == 1
    assert len(fake_store.finished_download_turns) == 1
    assert fake_store.gallery_ingest_requests == []


async def test_deep_cascade_exception_keeps_root_request_and_hands_off(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    seed = gallery(1)
    sibling = gallery(2)
    tag = SimpleNamespace(href="https://exhentai.org/tag/artist:someone")
    fake_driver.tag_results["artist"] = [cast(Tag, tag)]
    fake_driver.search_results[(tag.href, "")] = (sibling,)

    async def fail_sibling(target: GalleryURLParser) -> bool:
        if target.gid == sibling.gid:
            raise InsufficientFundsException("broke")
        return True

    fake_driver.download_result = fail_sibling
    downloader = downloader_factory(retry2download=0)

    with pytest.raises(InsufficientFundsException):
        await downloader.deep_download_by_gallery(
            seed,
            TagCascadePolicy(filters=("artist",), conditions=()),
        )

    assert seed.gid in fake_store.download_requests
    assert len(fake_store.gallery_ingest_requests) == 1
    assert fake_store.finished_download_turns == []


async def test_cancelled_deep_root_keeps_request_and_hands_off(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    download_started = asyncio.Event()
    download_cancelled = asyncio.Event()

    async def block_download(_gallery: GalleryURLParser) -> bool:
        download_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            download_cancelled.set()
        raise AssertionError("unreachable")

    fake_driver.download_result = block_download
    downloader = downloader_factory()
    task = asyncio.create_task(
        downloader.deep_download_by_gallery(
            gallery(1),
            TagCascadePolicy(filters=(), conditions=()),
        )
    )
    await download_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert download_cancelled.is_set()
    assert 1 in fake_store.download_requests
    assert len(fake_store.gallery_ingest_requests) == 1
    assert fake_store.finished_download_turns == []


async def test_long_deep_root_renews_its_turn_lease(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    renewed = asyncio.Event()
    fake_store.renew_download_turn_observer = renewed.set

    async def wait_for_renewal(_gallery: GalleryURLParser) -> bool:
        await renewed.wait()
        return True

    fake_driver.download_result = wait_for_renewal
    downloader = downloader_factory(
        turn_lease_seconds=1,
        turn_heartbeat_seconds=0.001,
    )

    result = await downloader.deep_download_by_gallery(
        gallery(1),
        TagCascadePolicy(filters=(), conditions=()),
    )

    assert result == {1: True}
    assert len(fake_store.renew_download_turn_calls) >= 1
    assert all(
        lease_seconds == 1
        for _turn, lease_seconds in fake_store.renew_download_turn_calls
    )


async def test_lost_turn_cancels_deep_root_but_still_attempts_handoff(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    download_started = asyncio.Event()
    download_cancelled = asyncio.Event()
    fake_store.renew_download_turn_result = False

    def expire_turn() -> None:
        fake_store.active_download_turn = None

    fake_store.renew_download_turn_observer = expire_turn

    async def block_download(_gallery: GalleryURLParser) -> bool:
        download_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            download_cancelled.set()
        raise AssertionError("unreachable")

    fake_driver.download_result = block_download
    downloader = downloader_factory(
        turn_lease_seconds=1,
        turn_heartbeat_seconds=0.001,
    )

    with pytest.raises(DownloadTurnLostError):
        await downloader.deep_download_by_gallery(
            gallery(1),
            TagCascadePolicy(filters=(), conditions=()),
        )

    assert download_started.is_set()
    assert download_cancelled.is_set()
    assert 1 in fake_store.download_requests
    assert len(fake_store.gallery_ingest_requests) == 1
    assert fake_store.finished_download_turns == []


async def test_late_stale_finish_cannot_delete_root_after_expired_turn_recovery(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
) -> None:
    def recover_expired_turn() -> None:
        turn = fake_store.active_download_turn
        assert turn is not None
        fake_store.completed_ingest_generation = turn.generation
        fake_store.active_download_turn = None
        fake_store.download_turn_available = True

    fake_store.finish_download_turn_observer = recover_expired_turn
    downloader = downloader_factory()

    with pytest.raises(DownloadTurnLostError):
        await downloader.deep_download_by_gallery(
            gallery(1),
            TagCascadePolicy(filters=(), conditions=()),
        )

    assert 1 in fake_store.download_requests
    assert len(fake_store.finished_download_turns) == 1
    assert fake_store.gallery_ingest_requests == []


async def test_atomic_finish_does_not_delete_newer_root_request_token(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    newer_request = DownloadRequest(1, gallery(1).url, "newer-token")

    async def replace_root_request(target: GalleryURLParser) -> bool:
        assert target.gid == 1
        old_request = fake_store.download_requests[1]
        assert old_request.token != newer_request.token
        fake_store.download_requests[1] = newer_request
        return True

    fake_driver.download_result = replace_root_request
    downloader = downloader_factory()

    assert await downloader.deep_download_by_gallery(
        gallery(1),
        TagCascadePolicy(filters=(), conditions=()),
    ) == {1: True}

    assert fake_store.download_requests == {1: newer_request}
    assert len(fake_store.finished_download_turns) == 1
    assert fake_store.gallery_ingest_requests == []
    assert fake_store.completed_ingest_generation == 1
    _turn, finished_request = fake_store.finished_download_turns[0]
    assert finished_request.token != newer_request.token


async def test_drain_queue_hands_off_once_after_a_short_final_batch(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.auto_complete_gallery_ingest = False
    fake_store.download_requests = {
        gid: DownloadRequest(gid, gallery(gid).url, f"request-{gid}") for gid in (1, 2)
    }
    downloader = downloader_factory()
    task = asyncio.create_task(
        downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))
    )

    await wait_until(lambda: len(fake_store.gallery_ingest_requests) == 1)
    assert gids_of(fake_driver.download_calls) == [1, 2]
    assert len(fake_store.completed_download_requests_in_turn) == 2
    assert not task.done()

    fake_store.complete_gallery_ingest()

    assert await task == {1: True, 2: True}
    assert [
        turn.generation
        for turn, _request in fake_store.completed_download_requests_in_turn
    ] == [1, 1]
    assert len(fake_store.gallery_ingest_requests) == 1
    assert len(fake_store.claim_download_turn_calls) == 1


async def test_submission_threshold_soft_overshoot_finishes_the_whole_root(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.download_requests = {
        gid: DownloadRequest(gid, gallery(gid).url, f"request-{gid}")
        for gid in range(1, 5)
    }
    expected_download_gids = {1, 2, 3, 4}
    for root_gid, root_submission_count in ((1, 10), (2, 11), (3, 103)):
        tag = cast(
            Tag,
            SimpleNamespace(href=f"https://exhentai.org/tag/artist:root-{root_gid}"),
        )
        fake_driver.gallery_tag_results[(root_gid, "artist")] = [tag]
        related = tuple(
            gallery(root_gid * 1000 + offset)
            for offset in range(1, root_submission_count)
        )
        fake_driver.search_results[(tag.href, "")] = related
        expected_download_gids.update(item.gid for item in related)

    downloader = downloader_factory(download_submissions_per_ingest=100)

    result = await downloader.drain_queue(
        TagCascadePolicy(filters=("artist",), conditions=())
    )

    assert result == {gid: True for gid in expected_download_gids}
    assert len(fake_driver.download_calls) == 10 + 11 + 103 + 1
    assert [
        turn.generation
        for turn, _request in fake_store.completed_download_requests_in_turn
    ] == [1, 1, 1, 2]
    assert [turn.generation for turn in fake_store.gallery_ingest_requests] == [
        1,
        2,
    ]
    assert len(fake_store.claim_download_turn_calls) == 2


async def test_zero_submission_missing_and_keep_roots_do_not_reach_threshold(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    requests = {
        1: DownloadRequest(1, "", "request-1"),
        2: DownloadRequest(2, gallery(2).url, "request-2"),
        3: DownloadRequest(3, gallery(3).url, "request-3"),
    }
    fake_store.download_requests = requests.copy()
    fake_driver.lookup_results[1] = ConfirmedGalleryMissing(1, 2)
    fake_driver.lookup_results[2] = GalleryFound(2, gallery(2))
    fake_driver.lookup_results[3] = GalleryFound(3, gallery(3))
    fake_driver.download_result = False
    downloader = downloader_factory(download_submissions_per_ingest=1)

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {2: False, 3: False}
    assert fake_store.download_requests == {2: requests[2], 3: requests[3]}
    assert fake_store.removed_gids == {1}
    assert fake_store.completed_download_requests_in_turn == []
    assert [
        turn.generation
        for turn, _request, _gid in (
            fake_store.completed_missing_download_requests_in_turn
        )
    ] == [1]
    assert [turn.generation for turn in fake_store.gallery_ingest_requests] == [1]


async def test_stale_snapshot_entry_does_not_consume_a_batch_root_slot(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    requests = {
        gid: DownloadRequest(gid, gallery(gid).url, f"request-{gid}")
        for gid in (1, 2, 3)
    }
    fake_store.download_requests = requests.copy()
    replacement = DownloadRequest(2, gallery(2).url, "replacement-2")

    async def make_second_snapshot_token_stale(target: GalleryURLParser) -> bool:
        if target.gid == 1:
            fake_store.download_requests[2] = replacement
        return True

    fake_driver.download_result = make_second_snapshot_token_stale
    downloader = downloader_factory(download_submissions_per_ingest=2)

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {1: True, 3: True}
    assert gids_of(fake_driver.download_calls) == [1, 3]
    assert [
        (turn.generation, request.gid)
        for turn, request in fake_store.completed_download_requests_in_turn
    ] == [(1, 1), (1, 3)]
    assert fake_store.download_requests == {2: replacement}
    assert len(fake_store.gallery_ingest_requests) == 1


async def test_batch_exception_preserves_current_root_after_settling_prior_root(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    requests = {
        gid: DownloadRequest(gid, gallery(gid).url, f"request-{gid}")
        for gid in (1, 2, 3)
    }
    fake_store.download_requests = requests.copy()

    async def fail_second(target: GalleryURLParser) -> bool:
        if target.gid == 2:
            raise InsufficientFundsException("broke")
        return True

    fake_driver.download_result = fail_second
    downloader = downloader_factory(retry2download=0)

    with pytest.raises(InsufficientFundsException):
        await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert fake_store.download_requests == {2: requests[2], 3: requests[3]}
    assert [
        request.gid for _turn, request in fake_store.completed_download_requests_in_turn
    ] == [1]
    assert gids_of(fake_driver.download_calls) == [1, 2]
    assert len(fake_store.gallery_ingest_requests) == 1
    assert fake_store.completed_ingest_generation == 1


async def test_batch_cancellation_preserves_current_root_after_settling_prior_root(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    requests = {
        gid: DownloadRequest(gid, gallery(gid).url, f"request-{gid}")
        for gid in (1, 2, 3)
    }
    fake_store.download_requests = requests.copy()
    second_started = asyncio.Event()
    second_cancelled = asyncio.Event()

    async def block_second(target: GalleryURLParser) -> bool:
        if target.gid == 2:
            second_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                second_cancelled.set()
        return True

    fake_driver.download_result = block_second
    downloader = downloader_factory()
    task = asyncio.create_task(
        downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))
    )
    await second_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert second_cancelled.is_set()
    assert fake_store.download_requests == {2: requests[2], 3: requests[3]}
    assert [
        request.gid for _turn, request in fake_store.completed_download_requests_in_turn
    ] == [1]
    assert len(fake_store.gallery_ingest_requests) == 1
    assert fake_store.completed_ingest_generation == 1


async def test_one_heartbeat_spans_a_whole_zero_submission_batch(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    fake_store.download_requests = {
        gid: DownloadRequest(gid, gallery(gid).url, f"request-{gid}") for gid in (1, 2)
    }
    fake_driver.lookup_results = {
        gid: GalleryFound(gid, gallery(gid)) for gid in (1, 2)
    }
    renewed = asyncio.Event()
    fake_store.renew_download_turn_observer = renewed.set

    async def wait_during_second_root(target: GalleryURLParser) -> bool:
        if target.gid == 2:
            await renewed.wait()
        return False

    fake_driver.download_result = wait_during_second_root
    downloader = downloader_factory(
        turn_lease_seconds=1,
        turn_heartbeat_seconds=0.001,
        download_submissions_per_ingest=1,
    )

    result = await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert result == {1: False, 2: False}
    assert len(fake_store.claim_download_turn_calls) == 1
    assert len(fake_store.renew_download_turn_calls) >= 1
    assert {turn for turn, _lease_seconds in fake_store.renew_download_turn_calls} == {
        fake_store.gallery_ingest_requests[0]
    }
    assert fake_store.completed_download_requests_in_turn == []


async def test_related_queued_root_is_submitted_once_but_runs_its_later_cascade(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    root_one = DownloadRequest(1, gallery(1).url, "request-1")
    root_two = DownloadRequest(2, gallery(2).url, "request-2")
    fake_store.download_requests = {1: root_one, 2: root_two}
    tag = cast(
        Tag,
        SimpleNamespace(href="https://exhentai.org/tag/artist:someone"),
    )
    fake_driver.tag_results["artist"] = [tag]
    fake_driver.search_results[(tag.href, "")] = (gallery(2), gallery(3))
    search_count = 0

    def assert_root_token_survives_related_download(_request: SearchRequest) -> None:
        nonlocal search_count
        search_count += 1
        if search_count == 2:
            assert fake_store.download_requests[2] == root_two

    fake_driver.search_observer = assert_root_token_survives_related_download
    downloader = downloader_factory(download_submissions_per_ingest=1)

    result = await downloader.drain_queue(
        TagCascadePolicy(filters=("artist",), conditions=()),
        skip_check=False,
    )

    assert result == {1: True, 2: True, 3: True}
    assert gids_of(fake_driver.download_calls) == [1, 2, 3]
    assert search_count == 2
    assert fake_store.download_requests == {}
    assert [
        (turn.generation, request.gid)
        for turn, request in fake_store.completed_download_requests_in_turn
    ] == [(1, 1), (2, 2)]
    assert len(fake_store.gallery_ingest_requests) == 2


async def test_empty_or_all_stale_snapshot_does_not_claim_a_turn(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    policy = TagCascadePolicy(filters=(), conditions=())

    assert await downloader.drain_queue(policy) == {}

    stale = DownloadRequest(1, gallery(1).url, "stale-token")
    current = DownloadRequest(1, gallery(1).url, "current-token")
    fake_store.download_requests = {1: current}
    monkeypatch.setattr(downloader._queue, "download_requests", lambda: [stale])

    assert await downloader.drain_queue(policy) == {}
    assert fake_store.claim_download_turn_calls == []


async def test_lost_turn_rejects_in_turn_settlement_and_preserves_request(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
) -> None:
    request = DownloadRequest(1, gallery(1).url, "request-1")
    fake_store.download_requests = {1: request}

    def lose_turn_before_settlement() -> None:
        fake_store.active_download_turn = None

    fake_store.complete_download_request_in_turn_observer = lose_turn_before_settlement
    downloader = downloader_factory()

    with pytest.raises(DownloadTurnLostError):
        await downloader.drain_queue(TagCascadePolicy(filters=(), conditions=()))

    assert fake_store.download_requests == {1: request}
    assert len(fake_store.completed_download_requests_in_turn) == 1
    assert len(fake_store.gallery_ingest_requests) == 1


async def test_in_turn_completion_does_not_delete_a_newer_root_token(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    stale_request = DownloadRequest(1, gallery(1).url, "request-1")
    newer_request = DownloadRequest(1, gallery(1).url, "newer-request-1")
    second_request = DownloadRequest(2, gallery(2).url, "request-2")
    fake_store.download_requests = {1: stale_request, 2: second_request}

    async def replace_root_token(target: GalleryURLParser) -> bool:
        if target.gid == 1:
            fake_store.download_requests[1] = newer_request
        return True

    fake_driver.download_result = replace_root_token
    downloader = downloader_factory(download_submissions_per_ingest=1)

    assert await downloader.drain_queue(
        TagCascadePolicy(filters=(), conditions=())
    ) == {1: True, 2: True}

    assert fake_store.download_requests == {1: newer_request}
    assert [
        (turn.generation, request.gid)
        for turn, request in fake_store.completed_download_requests_in_turn
    ] == [(1, 1), (2, 2)]
    assert len(fake_store.gallery_ingest_requests) == 2


async def test_direct_download_apis_do_not_claim_an_ingest_turn(
    downloader_factory: Callable[..., Downloader],
    fake_store: FakeDBStore,
    fake_driver: FakeDriver,
) -> None:
    tag = cast(
        Tag,
        SimpleNamespace(href="https://exhentai.org/tag/artist:someone"),
    )
    fake_driver.lookup_results[2] = GalleryFound(2, gallery(2))
    fake_driver.search_results[(tag.href, "")] = ()
    downloader = downloader_factory()

    await downloader.download_by_gallery(gallery(1))
    await downloader.download_by_gid(2)
    await downloader.download_by_tag(tag, ())

    assert fake_store.claim_download_turn_calls == []
    assert fake_store.gallery_ingest_requests == []
    assert fake_store.finished_download_turns == []
