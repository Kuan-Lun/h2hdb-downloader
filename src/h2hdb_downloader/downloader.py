import asyncio
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from math import isfinite
from random import random

from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import DownloadRequest, load_config
from hbrowser import ExHDriver, Tag
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from ._queue import DownloadTurn, GalleryQueue


def _is_retryable_sqlite_lock_error(error: sqlite3.OperationalError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(error_code, int):
        return False
    primary_code = error_code & 0xFF
    return primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


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
class _RootDownloadResult:
    downloads: dict[int, bool]
    request_to_complete: DownloadRequest | None


class DownloadTurnLostError(RuntimeError):
    """The process no longer owns the download turn it claimed."""


class Downloader:
    """Drives an ``hbrowser`` session to download galleries and record them in h2hdb.

    This is the sole public entry point of this package. It owns a durable
    request queue internally (see ``_queue.GalleryQueue``). A request is
    completed only after its download succeeds or its gid is conclusively
    removed/redirected, so an interrupted or failed attempt remains resumable.
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
        driver: ExHDriver,
        config_path: str,
        csv_path: str | os.PathLike[str] | None = None,
        *,
        wait4client: int,
        retry2download: int,
        turn_poll_seconds: float = 5,
        turn_lease_seconds: int = 300,
        turn_heartbeat_seconds: float = 60,
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

        self.driver = driver
        self.wait4client = wait4client
        self.retry2download = retry2download
        self.turn_poll_seconds = turn_poll_seconds
        self.turn_lease_seconds = turn_lease_seconds
        self.turn_heartbeat_seconds = turn_heartbeat_seconds
        config = load_config(config_path)
        self._queue = GalleryQueue(config=config, csv_path=csv_path)

    async def __aenter__(self) -> Downloader:
        await self.driver.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.driver.__aexit__(*exc_info)

    async def download_by_gallery(
        self, target: GalleryURLParser | Iterable[GalleryURLParser]
    ) -> dict[int, bool]:
        """Download one known gallery, or several, with retry on transient errors.

        Results are keyed by gid rather than the ``GalleryURLParser`` itself,
        since that class isn't hashable.
        """
        return await self._download_by_gallery(target)

    async def _download_by_gallery(
        self, target: GalleryURLParser | Iterable[GalleryURLParser]
    ) -> dict[int, bool]:
        if isinstance(target, GalleryURLParser):
            return {target.gid: await self._download_one(target)}
        gb = dict[int, bool]()
        for gallery in target:
            gb[gallery.gid] = await self._download_one(gallery)
        return gb

    async def _download_one(
        self,
        gallery: GalleryURLParser,
        request: DownloadRequest | None = None,
        *,
        complete_on_success: bool = True,
    ) -> bool:
        async def raise_after_wait(wait_seconds: int, error: Exception) -> None:
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            else:
                raise error

        if request is None:
            if not self._queue.should_attempt(gallery.gid):
                self._queue.note_skip()
                return False
            active_request = self._queue.request_download(gallery.gid, gallery.url)
        else:
            active_request = request

        try:
            return await self._attempt_download(
                gallery,
                active_request,
                complete_on_success=complete_on_success,
            )
        except ClientOfflineException as e:
            await raise_after_wait(self.wait4client, e)
            if not self._queue.is_current(active_request):
                return False
            return await self._attempt_download(
                gallery,
                active_request,
                complete_on_success=complete_on_success,
            )
        except InsufficientFundsException as e:
            await raise_after_wait(self.retry2download, e)
            if not self._queue.is_current(active_request):
                return False
            return await self._attempt_download(
                gallery,
                active_request,
                complete_on_success=complete_on_success,
            )

    async def _attempt_download(
        self,
        gallery: GalleryURLParser,
        request: DownloadRequest,
        *,
        complete_on_success: bool,
    ) -> bool:
        downloaded = await self.driver.download(gallery)
        if downloaded:
            with self._queue._database_operation() as connector:
                if connector.gallery_gids.check_gid_by_gid(gallery.gid):
                    connector.update_redownload_time_to_now_by_gid(gallery.gid)
                if complete_on_success:
                    connector.complete_download_request(request)
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
        request: DownloadRequest,
    ) -> _RootDownloadResult:
        if not self._queue.is_current(request):
            return _RootDownloadResult({}, request_to_complete=None)

        gb = dict[int, bool]()
        request_to_complete = None
        galleries = await self.driver.search(f"gid:{gid}", isclear=True)
        match len(galleries):
            case 0:
                with self._queue._database_operation() as connector:
                    connector.removed_galleries.insert_removed_gallery_gid(gid)
                request_to_complete = request
            case 1:
                gallery = galleries[0]
                is_redirect = gallery.gid != gid
                downloaded = await self._download_one(
                    gallery,
                    request,
                    complete_on_success=False,
                )
                gb[gallery.gid] = downloaded
                if is_redirect and gb[gallery.gid]:
                    with self._queue._database_operation() as connector:
                        if connector.gallery_gids.check_gid_by_gid(gid):
                            connector.request_gallery_deletion(gid)
                if policy is not None and (downloaded or skip_check):
                    gb = _merge_results(
                        gb,
                        await self._download_related_galleries(gallery, policy),
                    )
                if downloaded:
                    request_to_complete = request
            case _:
                raise ValueError("There can only be one gallery or none.")
        return _RootDownloadResult(
            gb,
            request_to_complete=request_to_complete,
        )

    async def download_by_tag(
        self, tag: Tag, conditions: Sequence[str]
    ) -> dict[int, bool]:
        """Download every gallery under ``tag`` matching each of ``conditions``."""
        return await self._download_by_tag(tag, conditions)

    async def _download_by_tag(
        self, tag: Tag, conditions: Sequence[str]
    ) -> dict[int, bool]:
        gb = dict[int, bool]()
        searches = conditions or [""]
        for condition in searches:
            await self.driver.get(tag.href)
            galleries = await self.driver.search(condition, isclear=False)
            gb = _merge_results(gb, await self._download_by_gallery(galleries))
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
                    request_to_complete=None,
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
                request_to_complete=request,
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
        request: DownloadRequest,
    ) -> _RootDownloadResult:
        downloaded = await self._download_one(
            gallery,
            request,
            complete_on_success=False,
        )
        gb = {gallery.gid: downloaded}
        if downloaded or skip_check:
            gb = _merge_results(
                gb,
                await self._download_related_galleries(gallery, policy),
            )
        return _RootDownloadResult(
            gb,
            request_to_complete=request if downloaded else None,
        )

    async def _download_related_galleries(
        self,
        gallery: GalleryURLParser,
        policy: TagCascadePolicy,
    ) -> dict[int, bool]:
        gb = dict[int, bool]()
        for filter in policy.filters:
            taglist = await self.driver.gallery2tag(gallery, filter=filter)
            for tag in taglist:
                gb = _merge_results(
                    gb, await self._download_by_tag(tag, policy.conditions)
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
        """Process one live snapshot, handing off after every root request."""
        gb = dict[int, bool]()
        for request in self._queue.download_requests():
            if not self._queue.is_current(request):
                continue
            gb = _merge_results(
                gb,
                await self._run_coordinated_root(
                    partial(
                        self._drain_root_request,
                        request,
                        policy,
                        skip_check,
                    )
                ),
            )
        return gb

    async def _drain_root_request(
        self,
        request: DownloadRequest,
        policy: TagCascadePolicy,
        skip_check: bool,
    ) -> _RootDownloadResult:
        if not self._queue.is_current(request):
            return _RootDownloadResult({}, request_to_complete=None)

        if not request.url:
            return await self._resolve_and_download(
                request.gid,
                policy=policy,
                skip_check=skip_check,
                request=request,
            )

        gallery = GalleryURLParser(url=request.url)
        direct_outcome = await self._deep_download_by_gallery(
            gallery,
            policy,
            skip_check,
            request=request,
        )
        if direct_outcome.request_to_complete is not None:
            return direct_outcome

        # Downloading straight from a URL cannot identify a removed or
        # redirected gallery. The gid fallback is part of this same root turn.
        fallback_outcome = await self._resolve_and_download(
            gallery.gid,
            policy=policy,
            skip_check=skip_check,
            request=request,
        )
        return _RootDownloadResult(
            _merge_results(
                direct_outcome.downloads,
                fallback_outcome.downloads,
            ),
            request_to_complete=fallback_outcome.request_to_complete,
        )

    def _complete_direct_request(self, outcome: _RootDownloadResult) -> None:
        if outcome.request_to_complete is not None:
            self._queue.complete_download_request(outcome.request_to_complete)

    async def _claim_download_turn(self) -> DownloadTurn:
        while True:
            try:
                turn = self._queue.claim_download_turn(
                    lease_seconds=self.turn_lease_seconds
                )
            except sqlite3.OperationalError as error:
                if not _is_retryable_sqlite_lock_error(error):
                    raise
                turn = None
            if turn is not None:
                return turn
            await asyncio.sleep(self.turn_poll_seconds)

    async def _heartbeat_download_turn(
        self,
        turn: DownloadTurn,
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
                if not self._queue.renew_download_turn(
                    turn,
                    lease_seconds=self.turn_lease_seconds,
                ):
                    raise DownloadTurnLostError(
                        f"download turn generation {turn.generation} was lost"
                    )

    async def _run_with_turn_heartbeat(
        self,
        turn: DownloadTurn,
        operation: Callable[[], Awaitable[_RootDownloadResult]],
    ) -> _RootDownloadResult:
        stop = asyncio.Event()
        operation_task: asyncio.Future[_RootDownloadResult] = asyncio.ensure_future(
            operation()
        )
        heartbeat_task: asyncio.Task[None] = asyncio.create_task(
            self._heartbeat_download_turn(turn, stop)
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

    async def _wait_for_gallery_ingest(self, turn: DownloadTurn) -> None:
        while True:
            try:
                completed_generation = self._queue.completed_gallery_ingest_generation()
            except sqlite3.OperationalError as error:
                if not _is_retryable_sqlite_lock_error(error):
                    raise
            else:
                if completed_generation >= turn.generation:
                    return
            await asyncio.sleep(self.turn_poll_seconds)

    async def _run_coordinated_root(
        self,
        operation: Callable[[], Awaitable[_RootDownloadResult]],
    ) -> dict[int, bool]:
        turn = await self._claim_download_turn()
        try:
            result = await self._run_with_turn_heartbeat(turn, operation)
        except BaseException:
            self._queue.request_gallery_ingest(turn)
            raise

        if result.request_to_complete is None:
            handed_off = self._queue.request_gallery_ingest(turn)
        else:
            handed_off = self._queue.finish_download_turn(
                turn,
                result.request_to_complete,
            )

        if not handed_off:
            raise DownloadTurnLostError(
                f"download turn generation {turn.generation} was lost before handoff"
            )
        await self._wait_for_gallery_ingest(turn)
        return result.downloads
