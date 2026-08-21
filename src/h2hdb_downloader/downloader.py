import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from math import isfinite
from random import random
from types import TracebackType
from typing import Protocol, Self, TypeVar, assert_never

from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import (
    DownloadHandoff,
    DownloadIngestUnavailableError,
    DownloadTurn,
    VNextDownloadQueueFacade,
    VNextDownloadRequest,
)
from hbrowser import (
    ConfirmedGalleryMissing,
    GalleryFound,
    GalleryLookupResult,
    GallerySearchResult,
    SearchRequest,
    Tag,
)
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from ._queue import GalleryQueue

_T = TypeVar("_T")
_BatchItemT = TypeVar("_BatchItemT")

logger = logging.getLogger(__name__)


def _merge_results(dict1: dict[int, bool], dict2: dict[int, bool]) -> dict[int, bool]:
    return {
        key: dict1.get(key, False) or dict2.get(key, False)
        for key in set(dict1) | set(dict2)
    }


@dataclass(frozen=True, slots=True)
class TagCascadePolicy:
    """How to cascade from a gallery into its related works.

    ``filters`` are the tag categories to follow (e.g. ``"artist"``,
    ``"group"``); ``conditions`` are the search conditions applied within
    each of those tags (e.g. a language filter). Both always travel
    together wherever a deep download happens.
    """

    filters: tuple[str, ...]
    conditions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _KeepRequest:
    """The durable root request must remain queued."""


@dataclass(frozen=True, slots=True)
class _CompleteRequest:
    """The durable root request completed successfully."""

    request: VNextDownloadRequest


@dataclass(frozen=True, slots=True)
class _ConfirmMissing:
    """The requested GID was conclusively absent."""

    request: VNextDownloadRequest
    gid: int


type _RootDisposition = _KeepRequest | _CompleteRequest | _ConfirmMissing

_KEEP_REQUEST = _KeepRequest()


@dataclass(frozen=True, slots=True)
class _RootDownloadResult:
    downloads: dict[int, bool]
    disposition: _RootDisposition
    processed: bool = True


@dataclass(slots=True)
class _DownloadBatchContext:
    """Submissions remembered across every turn in one public batch drain."""

    submitted_gids: set[int] = field(default_factory=set)

    def was_submitted(self, gid: int) -> bool:
        return gid in self.submitted_gids

    def note_submission(self, gid: int) -> None:
        self.submitted_gids.add(gid)


@dataclass(slots=True)
class _DownloadTurnLease:
    """The latest exact lease receipt shared with heartbeat and worker calls."""

    turn: DownloadTurn


@dataclass(frozen=True, slots=True)
class _BatchDownloadResult:
    downloads: dict[int, bool]
    next_snapshot_index: int


class DownloadTurnLostError(RuntimeError):
    """The process no longer owns the download turn it claimed."""


class GalleryDriver(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def download(self, gallery: GalleryURLParser) -> bool: ...

    async def lookup_gid(self, gid: int) -> GalleryLookupResult: ...

    async def search(self, request: SearchRequest) -> GallerySearchResult: ...

    async def gallery2tag(
        self,
        gallery: GalleryURLParser,
        filter: str,
    ) -> list[Tag]: ...


class Downloader:
    """Drives an ``hbrowser`` session to download galleries and record them in h2hdb.

    This is the sole public entry point of this package. It owns a durable
    request queue internally (see ``_queue.GalleryQueue``). A request is
    completed only after its download succeeds or its gid is conclusively
    removed/redirected, so an interrupted or failed attempt remains resumable.
    ``facade`` is an initialized, schema-compatible h2hdb public facade; its
    configuration and migrations remain the calling application's concern.
    ``csv_path`` is optional: it only enables the manual "queue a gid/url by
    editing a CSV file" feature — leave it as ``None`` if you don't need that.

    ``driver`` is taken un-entered; ``Downloader`` is itself an async context
    manager that opens and closes the browser session for you::

        async with Downloader(ExHDriver(headless=False), ...) as downloader:
            ...

    If you'd rather manage the driver's lifecycle yourself, pass an
    already-entered driver and skip ``async with downloader``.
    """

    def __init__(
        self,
        driver: GalleryDriver,
        facade: VNextDownloadQueueFacade,
        csv_path: str | os.PathLike[str] | None = None,
        *,
        wait4client: int,
        retry2download: int,
        turn_poll_seconds: float = 5,
        turn_lease_seconds: int = 300,
        turn_heartbeat_seconds: float = 60,
        download_submissions_per_ingest: int = 100,
    ) -> None:
        if not isfinite(turn_poll_seconds) or turn_poll_seconds <= 0:
            raise ValueError("turn_poll_seconds must be finite and greater than zero")
        if (
            isinstance(turn_lease_seconds, bool)
            or not isinstance(turn_lease_seconds, int)
            or turn_lease_seconds <= 0
        ):
            raise ValueError("turn_lease_seconds must be a positive integer")
        if not isfinite(turn_heartbeat_seconds) or turn_heartbeat_seconds <= 0:
            raise ValueError(
                "turn_heartbeat_seconds must be finite and greater than zero"
            )
        if turn_heartbeat_seconds >= turn_lease_seconds:
            raise ValueError(
                "turn_heartbeat_seconds must be shorter than turn_lease_seconds"
            )
        if (
            isinstance(download_submissions_per_ingest, bool)
            or not isinstance(download_submissions_per_ingest, int)
            or download_submissions_per_ingest <= 0
        ):
            raise ValueError(
                "download_submissions_per_ingest must be a positive integer"
            )

        self.driver = driver
        self.wait4client = wait4client
        self.retry2download = retry2download
        self.turn_poll_seconds = turn_poll_seconds
        self.turn_lease_seconds = turn_lease_seconds
        self.turn_heartbeat_seconds = turn_heartbeat_seconds
        self.download_submissions_per_ingest = download_submissions_per_ingest
        self._queue = GalleryQueue(facade=facade, csv_path=csv_path)

    async def __aenter__(self) -> Downloader:
        await self.driver.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.driver.__aexit__(exc_type, exc_value, traceback)

    async def download_by_gallery(
        self, target: GalleryURLParser | Iterable[GalleryURLParser]
    ) -> dict[int, bool]:
        """Download one known gallery, or several, with retry on transient errors.

        Results are keyed by gid rather than the ``GalleryURLParser`` itself,
        since that class isn't hashable.
        """
        return await self._download_by_gallery(target)

    async def _download_by_gallery(
        self,
        target: GalleryURLParser | Iterable[GalleryURLParser],
        *,
        batch_context: _DownloadBatchContext | None = None,
        preserve_existing_request: bool = False,
    ) -> dict[int, bool]:
        if isinstance(target, GalleryURLParser):
            return {
                target.gid: await self._download_one(
                    target,
                    batch_context=batch_context,
                    preserve_existing_request=preserve_existing_request,
                )
            }
        gb = dict[int, bool]()
        for gallery in target:
            gb[gallery.gid] = await self._download_one(
                gallery,
                batch_context=batch_context,
                preserve_existing_request=preserve_existing_request,
            )
        return gb

    async def _download_one(
        self,
        gallery: GalleryURLParser,
        request: VNextDownloadRequest | None = None,
        *,
        complete_on_success: bool = True,
        batch_context: _DownloadBatchContext | None = None,
        preserve_existing_request: bool = False,
    ) -> bool:
        async def raise_after_wait(wait_seconds: int, error: Exception) -> None:
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            else:
                raise error

        if batch_context is not None and batch_context.was_submitted(gallery.gid):
            return False

        should_complete_request = complete_on_success
        if request is None:
            if not self._queue.should_attempt(gallery.gid):
                self._queue.note_skip()
                return False
            if preserve_existing_request:
                ensured = self._queue.ensure_download_request(
                    gallery.gid,
                    gallery.url,
                )
                active_request = ensured.request
                should_complete_request = complete_on_success and ensured.created
            else:
                active_request = self._queue.request_download(
                    gallery.gid,
                    gallery.url,
                )
        else:
            active_request = request

        try:
            return await self._attempt_download(
                gallery,
                active_request,
                complete_on_success=should_complete_request,
                batch_context=batch_context,
            )
        except ClientOfflineException as e:
            await raise_after_wait(self.wait4client, e)
            if not self._queue.is_current(active_request):
                return False
            return await self._attempt_download(
                gallery,
                active_request,
                complete_on_success=should_complete_request,
                batch_context=batch_context,
            )
        except InsufficientFundsException as e:
            await raise_after_wait(self.retry2download, e)
            if not self._queue.is_current(active_request):
                return False
            return await self._attempt_download(
                gallery,
                active_request,
                complete_on_success=should_complete_request,
                batch_context=batch_context,
            )

    async def _attempt_download(
        self,
        gallery: GalleryURLParser,
        request: VNextDownloadRequest,
        *,
        complete_on_success: bool,
        batch_context: _DownloadBatchContext | None,
    ) -> bool:
        downloaded = await self.driver.download(gallery)
        if downloaded:
            self._queue.record_gallery_found(gallery.gid)
            if complete_on_success:
                self._queue.complete_download_request(request)
            if batch_context is not None:
                batch_context.note_submission(gallery.gid)
            await asyncio.sleep(random())
            self._queue.note_download_success()
        return downloaded

    async def download_by_gid(self, gid: int) -> dict[int, bool]:
        """Resolve a bare gid to its gallery (via search) and download it.

        The gid request is durable before the search starts. It is completed
        after a successful download, confirmed removal, or successful
        redirect; failures and interruptions leave it queued for retry.
        """
        request = self._queue.request_download(gid)
        outcome = await self._resolve_and_download(gid, request=request)
        self._complete_direct_request(outcome)
        return outcome.downloads

    async def _resolve_and_download(
        self,
        gid: int,
        *,
        policy: TagCascadePolicy | None = None,
        skip_check: bool = False,
        request: VNextDownloadRequest,
        batch_context: _DownloadBatchContext | None = None,
    ) -> _RootDownloadResult:
        if not self._queue.is_current(request):
            return _RootDownloadResult(
                {},
                disposition=_KEEP_REQUEST,
                processed=False,
            )

        gb = dict[int, bool]()
        disposition: _RootDisposition = _KEEP_REQUEST
        lookup = await self.driver.lookup_gid(gid)
        match lookup:
            case ConfirmedGalleryMissing(gid=missing_gid):
                if missing_gid != gid:
                    raise RuntimeError(
                        "Gallery lookup returned a missing result for the wrong GID "
                        f"(requested={gid}, returned={missing_gid})"
                    )
                disposition = _ConfirmMissing(request, gid)
            case GalleryFound(requested_gid=requested_gid, gallery=gallery):
                if requested_gid != gid:
                    raise RuntimeError(
                        "Gallery lookup returned a result for the wrong GID "
                        f"(requested={gid}, returned={requested_gid})"
                    )
                # The lookup proves both the requested route and its resolved
                # gallery are live. Clear any stale false-missing markers even
                # when the following download is unsuccessful.
                self._queue.record_gallery_found(gid, gallery.gid)
                is_redirect = gallery.gid != gid
                was_submitted = (
                    batch_context is not None
                    and batch_context.was_submitted(gallery.gid)
                )
                downloaded = await self._download_one(
                    gallery,
                    request,
                    complete_on_success=False,
                    batch_context=batch_context,
                )
                gb[gallery.gid] = downloaded
                root_completed = downloaded or was_submitted
                if is_redirect and root_completed:
                    if self._queue.is_cataloged(gid):
                        self._queue.request_gallery_deletion(gid)
                if policy is not None and (root_completed or skip_check):
                    gb = _merge_results(
                        gb,
                        await self._download_related_galleries(
                            gallery,
                            policy,
                            batch_context=batch_context,
                        ),
                    )
                if root_completed:
                    disposition = _CompleteRequest(request)
            case _:
                assert_never(lookup)
        return _RootDownloadResult(
            gb,
            disposition=disposition,
        )

    async def download_by_tag(
        self, tag: Tag, conditions: Sequence[str]
    ) -> dict[int, bool]:
        """Download every gallery under ``tag`` matching each of ``conditions``."""
        return await self._download_by_tag(tag, conditions)

    async def _download_by_tag(
        self,
        tag: Tag,
        conditions: Sequence[str],
        *,
        batch_context: _DownloadBatchContext | None = None,
        preserve_existing_request: bool = False,
    ) -> dict[int, bool]:
        gb = dict[int, bool]()
        searches = conditions or [""]
        for condition in searches:
            result = await self.driver.search(
                SearchRequest(
                    scope_url=tag.href,
                    query=condition,
                )
            )
            gb = _merge_results(
                gb,
                await self._download_by_gallery(
                    result.galleries,
                    batch_context=batch_context,
                    preserve_existing_request=preserve_existing_request,
                ),
            )
        return gb

    async def deep_download_by_gallery(
        self,
        gallery: GalleryURLParser,
        policy: TagCascadePolicy,
        skip_check: bool = False,
    ) -> dict[int, bool]:
        """Download ``gallery``, then cascade into its artist/group tags.

        ``skip_check`` forces the cascade to run even when ``gallery`` itself
        was skipped as already-settled (e.g. because it was just downloaded
        moments ago by a separate call). The whole cascade is one coordinated
        root and a normal return waits for h2hdb to ingest its generation.
        """
        return await self._run_coordinated_root(
            lambda: self._run_deep_gallery_root(gallery, policy, skip_check)
        )

    async def _run_deep_gallery_root(
        self,
        gallery: GalleryURLParser,
        policy: TagCascadePolicy,
        skip_check: bool,
    ) -> _RootDownloadResult:
        if not self._queue.should_attempt(gallery.gid):
            self._queue.note_skip()
            if not skip_check:
                return _RootDownloadResult(
                    {gallery.gid: False},
                    disposition=_KEEP_REQUEST,
                )

            # The root is already settled, but the related-tag traversal is
            # still a durable root job: interruption must leave something
            # drain_queue() can resume.
            request = self._queue.request_download(gallery.gid, gallery.url)
            downloads = _merge_results(
                {gallery.gid: False},
                await self._download_related_galleries(gallery, policy),
            )
            return _RootDownloadResult(
                downloads,
                disposition=_CompleteRequest(request),
            )

        request = self._queue.request_download(gallery.gid, gallery.url)
        return await self._deep_download_by_gallery(
            gallery,
            policy,
            skip_check,
            request=request,
        )

    async def _deep_download_by_gallery(
        self,
        gallery: GalleryURLParser,
        policy: TagCascadePolicy,
        skip_check: bool,
        *,
        request: VNextDownloadRequest,
        batch_context: _DownloadBatchContext | None = None,
    ) -> _RootDownloadResult:
        was_submitted = batch_context is not None and batch_context.was_submitted(
            gallery.gid
        )
        downloaded = await self._download_one(
            gallery,
            request,
            complete_on_success=False,
            batch_context=batch_context,
        )
        gb = {gallery.gid: downloaded}
        root_completed = downloaded or was_submitted
        if root_completed or skip_check:
            gb = _merge_results(
                gb,
                await self._download_related_galleries(
                    gallery,
                    policy,
                    batch_context=batch_context,
                ),
            )
        return _RootDownloadResult(
            gb,
            disposition=(
                _CompleteRequest(request) if root_completed else _KEEP_REQUEST
            ),
        )

    async def _download_related_galleries(
        self,
        gallery: GalleryURLParser,
        policy: TagCascadePolicy,
        *,
        batch_context: _DownloadBatchContext | None = None,
    ) -> dict[int, bool]:
        gb = dict[int, bool]()
        for filter in policy.filters:
            taglist = await self.driver.gallery2tag(gallery, filter=filter)
            for tag in taglist:
                gb = _merge_results(
                    gb,
                    await self._download_by_tag(
                        tag,
                        policy.conditions,
                        batch_context=batch_context,
                        preserve_existing_request=True,
                    ),
                )
        return gb

    async def deep_download_by_gid(
        self,
        gid: int,
        policy: TagCascadePolicy,
        skip_check: bool = False,
    ) -> dict[int, bool]:
        """Resolve and deep-download one coordinated root gid.

        The method hands the completed or interrupted turn to h2hdb, and a
        normal return waits for that generation's ingest to finish.
        """
        return await self._run_coordinated_root(
            lambda: self._run_deep_gid_root(gid, policy, skip_check)
        )

    async def _run_deep_gid_root(
        self,
        gid: int,
        policy: TagCascadePolicy,
        skip_check: bool,
    ) -> _RootDownloadResult:
        request = self._queue.request_download(gid)
        return await self._resolve_and_download(
            gid,
            policy=policy,
            skip_check=skip_check,
            request=request,
        )

    def pending_redownload_gids(self) -> list[int]:
        """Gids h2hdb currently flags as needing a redownload, oldest first."""
        return self._queue.pending_redownload_gids()

    async def drain_queue(
        self, policy: TagCascadePolicy, skip_check: bool = True
    ) -> dict[int, bool]:
        """Drain one durable-request snapshot with submission-count barriers."""

        snapshot = self._queue.download_requests()
        return await self._drain_batched_snapshot(
            snapshot,
            is_current=self._queue.is_current,
            run_root=lambda request, batch_context: self._drain_root_request(
                request,
                policy,
                skip_check,
                batch_context,
            ),
        )

    async def drain_pending_redownloads(
        self,
        policy: TagCascadePolicy,
        skip_check: bool = True,
    ) -> dict[int, bool]:
        """Drain one pending-redownload snapshot with the same ingest barriers."""

        # Publish every snapshot root before network work. If this process is
        # interrupted while seeding, the already-created exact-token requests
        # are recoverable through drain_queue(), while unseeded gids remain in
        # the pending-redownload view. Related downloads must therefore reuse,
        # rather than complete, a later pending root's durable request.
        snapshot = [
            self._queue.ensure_download_request(gid).request
            for gid in self._queue.pending_redownload_gids()
        ]
        return await self._drain_batched_snapshot(
            snapshot,
            is_current=self._queue.is_current,
            run_root=lambda request, batch_context: self._drain_pending_redownload_root(
                request,
                policy,
                skip_check,
                batch_context,
            ),
        )

    async def _drain_pending_redownload_root(
        self,
        request: VNextDownloadRequest,
        policy: TagCascadePolicy,
        skip_check: bool,
        batch_context: _DownloadBatchContext,
    ) -> _RootDownloadResult | None:
        if not self._queue.is_current(request):
            return None

        return await self._resolve_and_download(
            request.gid,
            policy=policy,
            skip_check=skip_check,
            request=request,
            batch_context=batch_context,
        )

    async def _drain_batched_snapshot(
        self,
        snapshot: Sequence[_BatchItemT],
        *,
        is_current: Callable[[_BatchItemT], bool],
        run_root: Callable[
            [_BatchItemT, _DownloadBatchContext],
            Awaitable[_RootDownloadResult | None],
        ],
    ) -> dict[int, bool]:
        """Run indivisible roots up to a soft accepted-submission threshold."""

        gb = dict[int, bool]()
        snapshot_index = 0
        batch_context = _DownloadBatchContext()
        while snapshot_index < len(snapshot):
            # Claim lazily: an empty snapshot, or one whose tokens have all
            # become stale, must not trigger a pointless full ingest scan.
            while snapshot_index < len(snapshot):
                item = snapshot[snapshot_index]
                if is_current(item):
                    break
                snapshot_index += 1
            if snapshot_index == len(snapshot):
                break

            batch_result = await self._run_coordinated_batch(
                snapshot,
                snapshot_index,
                batch_context,
                is_current,
                run_root,
            )
            snapshot_index = batch_result.next_snapshot_index
            gb = _merge_results(gb, batch_result.downloads)
        return gb

    async def _drain_root_request(
        self,
        request: VNextDownloadRequest,
        policy: TagCascadePolicy,
        skip_check: bool,
        batch_context: _DownloadBatchContext,
    ) -> _RootDownloadResult | None:
        if not self._queue.is_current(request):
            return None

        if not request.url:
            return await self._resolve_and_download(
                request.gid,
                policy=policy,
                skip_check=skip_check,
                request=request,
                batch_context=batch_context,
            )

        try:
            gallery = GalleryURLParser(url=request.url)
        except ValueError:
            logger.error(
                "Stored download request for GID %d has an invalid URL; "
                "ignoring the URL and resolving the requested GID",
                request.gid,
            )
            return await self._resolve_and_download(
                request.gid,
                policy=policy,
                skip_check=skip_check,
                request=request,
                batch_context=batch_context,
            )
        if gallery.gid != request.gid:
            logger.error(
                "Stored download request GID %d has a URL for GID %d; "
                "ignoring the URL and resolving the requested GID",
                request.gid,
                gallery.gid,
            )
            return await self._resolve_and_download(
                request.gid,
                policy=policy,
                skip_check=skip_check,
                request=request,
                batch_context=batch_context,
            )
        direct_outcome = await self._deep_download_by_gallery(
            gallery,
            policy,
            skip_check,
            request=request,
            batch_context=batch_context,
        )
        if isinstance(direct_outcome.disposition, _CompleteRequest):
            return direct_outcome

        # Downloading straight from a URL cannot identify a removed or
        # redirected gallery. The gid fallback is part of this same root turn.
        fallback_outcome = await self._resolve_and_download(
            gallery.gid,
            policy=policy,
            skip_check=skip_check,
            request=request,
            batch_context=batch_context,
        )
        return _RootDownloadResult(
            _merge_results(
                direct_outcome.downloads,
                fallback_outcome.downloads,
            ),
            disposition=fallback_outcome.disposition,
        )

    def _complete_direct_request(self, outcome: _RootDownloadResult) -> None:
        match outcome.disposition:
            case _KeepRequest():
                return
            case _CompleteRequest(request):
                self._queue.complete_download_request(request)
            case _ConfirmMissing(request, gid):
                self._queue.complete_missing_download_request(request, gid)
            case _:
                assert_never(outcome.disposition)

    async def _claim_download_turn(self) -> _DownloadTurnLease:
        while True:
            try:
                turn = self._queue.claim_download_turn(
                    lease_seconds=self.turn_lease_seconds
                )
            except DownloadIngestUnavailableError:
                pass
            else:
                return _DownloadTurnLease(turn)
            await asyncio.sleep(self.turn_poll_seconds)

    async def _heartbeat_download_turn(
        self,
        lease: _DownloadTurnLease,
        stop: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.turn_heartbeat_seconds,
                )
                return
            except TimeoutError:
                try:
                    lease.turn = self._queue.renew_download_turn(
                        lease.turn,
                        lease_seconds=self.turn_lease_seconds,
                    )
                except DownloadIngestUnavailableError as error:
                    raise DownloadTurnLostError(
                        f"download turn generation {lease.turn.generation} was lost"
                    ) from error

    async def _run_with_turn_heartbeat(
        self,
        lease: _DownloadTurnLease,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        stop = asyncio.Event()
        operation_task: asyncio.Future[_T] = asyncio.ensure_future(operation())
        heartbeat_task: asyncio.Task[None] = asyncio.create_task(
            self._heartbeat_download_turn(lease, stop)
        )
        try:
            done, _ = await asyncio.wait(
                (operation_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return await operation_task
            await heartbeat_task
            raise AssertionError("download-turn heartbeat stopped unexpectedly")
        finally:
            stop.set()
            if not operation_task.done():
                operation_task.cancel()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.gather(
                operation_task,
                heartbeat_task,
                return_exceptions=True,
            )

    async def _wait_for_gallery_ingest(self, handoff: DownloadHandoff) -> None:
        while True:
            try:
                completed = self._queue.is_download_handoff_complete(handoff)
            except DownloadIngestUnavailableError:
                pass
            else:
                if completed:
                    return
            await asyncio.sleep(self.turn_poll_seconds)

    def _complete_root_in_turn(
        self,
        lease: _DownloadTurnLease,
        disposition: _RootDisposition,
    ) -> None:
        try:
            match disposition:
                case _KeepRequest():
                    return
                case _CompleteRequest(request):
                    self._queue.complete_download_request_in_turn(
                        lease.turn,
                        request,
                    )
                case _ConfirmMissing(request, gid):
                    self._queue.complete_missing_download_request_in_turn(
                        lease.turn,
                        request,
                        gid,
                    )
                case _:
                    assert_never(disposition)
        except DownloadIngestUnavailableError as error:
            raise DownloadTurnLostError(
                f"download turn generation {lease.turn.generation} was lost "
                "while settling a batch root"
            ) from error

    async def _run_batch_roots(
        self,
        lease: _DownloadTurnLease,
        snapshot: Sequence[_BatchItemT],
        snapshot_index: int,
        batch_context: _DownloadBatchContext,
        is_current: Callable[[_BatchItemT], bool],
        run_root: Callable[
            [_BatchItemT, _DownloadBatchContext],
            Awaitable[_RootDownloadResult | None],
        ],
    ) -> _BatchDownloadResult:
        downloads = dict[int, bool]()
        submissions_at_batch_start = len(batch_context.submitted_gids)
        while snapshot_index < len(snapshot):
            item = snapshot[snapshot_index]
            snapshot_index += 1
            if not is_current(item):
                continue

            outcome = await run_root(item, batch_context)
            if outcome is None or not outcome.processed:
                continue
            downloads = _merge_results(downloads, outcome.downloads)
            # This synchronous fenced write is deliberately adjacent to the
            # normal traversal return. A later exception or process shutdown
            # cannot make an already-finished root depend on the rest of the
            # batch completing.
            self._complete_root_in_turn(lease, outcome.disposition)
            accepted_unique_submissions = (
                len(batch_context.submitted_gids) - submissions_at_batch_start
            )
            if accepted_unique_submissions >= self.download_submissions_per_ingest:
                break
        return _BatchDownloadResult(downloads, snapshot_index)

    async def _run_coordinated_batch(
        self,
        snapshot: Sequence[_BatchItemT],
        snapshot_index: int,
        batch_context: _DownloadBatchContext,
        is_current: Callable[[_BatchItemT], bool],
        run_root: Callable[
            [_BatchItemT, _DownloadBatchContext],
            Awaitable[_RootDownloadResult | None],
        ],
    ) -> _BatchDownloadResult:
        lease = await self._claim_download_turn()
        try:
            downloads = await self._run_with_turn_heartbeat(
                lease,
                lambda: self._run_batch_roots(
                    lease,
                    snapshot,
                    snapshot_index,
                    batch_context,
                    is_current,
                    run_root,
                ),
            )
        except BaseException as error:
            try:
                self._queue.handoff_download_turn(lease.turn)
            except DownloadIngestUnavailableError:
                raise DownloadTurnLostError(
                    f"download turn generation {lease.turn.generation} was lost "
                    "while handing off an interrupted batch"
                ) from error
            raise

        try:
            handoff = self._queue.handoff_download_turn(lease.turn)
        except DownloadIngestUnavailableError as error:
            raise DownloadTurnLostError(
                f"download turn generation {lease.turn.generation} was lost "
                "before handoff"
            ) from error
        await self._wait_for_gallery_ingest(handoff)
        return downloads

    async def _run_coordinated_root(
        self,
        operation: Callable[[], Awaitable[_RootDownloadResult]],
    ) -> dict[int, bool]:
        lease = await self._claim_download_turn()
        try:
            result = await self._run_with_turn_heartbeat(lease, operation)
        except BaseException as error:
            try:
                self._queue.handoff_download_turn(lease.turn)
            except DownloadIngestUnavailableError:
                raise DownloadTurnLostError(
                    f"download turn generation {lease.turn.generation} was lost "
                    "while handing off a failed root"
                ) from error
            raise

        try:
            match result.disposition:
                case _KeepRequest():
                    handoff = self._queue.handoff_download_turn(lease.turn)
                case _CompleteRequest(request):
                    handoff = self._queue.finish_download_turn(lease.turn, request)
                case _ConfirmMissing(request, gid):
                    handoff = self._queue.finish_missing_download_turn(
                        lease.turn,
                        request,
                        gid,
                    )
                case _:
                    assert_never(result.disposition)
        except DownloadIngestUnavailableError as error:
            raise DownloadTurnLostError(
                f"download turn generation {lease.turn.generation} was lost "
                "before handoff"
            ) from error
        await self._wait_for_gallery_ingest(handoff)
        return result.downloads
