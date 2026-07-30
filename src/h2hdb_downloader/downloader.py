import asyncio
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from random import random

from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import H2HDB, DownloadRequest, load_config
from hbrowser import ExHDriver, Tag
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from ._queue import GalleryQueue


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
    ) -> None:
        self.driver = driver
        self.wait4client = wait4client
        self.retry2download = retry2download
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
            with H2HDB(config=self._queue.config) as connector:
                if connector.gallery_gids.check_gid_by_gid(gallery.gid):
                    connector.update_redownload_time_to_now_by_gid(gallery.gid)
            if complete_on_success:
                self._queue.complete_download_request(request)
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
        return await self._resolve_and_download(gid, request=request)

    async def _resolve_and_download(
        self,
        gid: int,
        *,
        policy: TagCascadePolicy | None = None,
        skip_check: bool = False,
        request: DownloadRequest,
    ) -> dict[int, bool]:
        if not self._queue.is_current(request):
            return {}

        gb = dict[int, bool]()
        galleries = await self.driver.search(f"gid:{gid}", isclear=True)
        match len(galleries):
            case 0:
                with H2HDB(config=self._queue.config) as connector:
                    connector.removed_galleries.insert_removed_gallery_gid(gid)
                self._queue.complete_download_request(request)
            case 1:
                gallery = galleries[0]
                is_redirect = gallery.gid != gid
                downloaded = await self._download_one(
                    gallery,
                    request,
                    complete_on_success=not is_redirect,
                )
                gb[gallery.gid] = downloaded
                if is_redirect and gb[gallery.gid]:
                    with H2HDB(config=self._queue.config) as connector:
                        if connector.gallery_gids.check_gid_by_gid(gid):
                            connector.request_gallery_deletion(gid)
                    self._queue.complete_download_request(request)
                if policy is not None and (downloaded or skip_check):
                    gb = _merge_results(
                        gb,
                        await self._download_related_galleries(gallery, policy),
                    )
            case _:
                raise ValueError("There can only be one gallery or none.")
        return gb

    async def download_by_tag(
        self, tag: Tag, conditions: Sequence[str]
    ) -> dict[int, bool]:
        """Download every gallery under ``tag`` matching each of ``conditions``."""
        gb = dict[int, bool]()
        searches = conditions or [""]
        for condition in searches:
            await self.driver.get(tag.href)
            galleries = await self.driver.search(condition, isclear=False)
            gb = _merge_results(gb, await self.download_by_gallery(galleries))
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
        moments ago by a separate call).
        """
        return await self._deep_download_by_gallery(gallery, policy, skip_check)

    async def _deep_download_by_gallery(
        self,
        gallery: GalleryURLParser,
        policy: TagCascadePolicy,
        skip_check: bool,
        *,
        request: DownloadRequest | None = None,
        complete_on_success: bool = True,
    ) -> dict[int, bool]:
        downloaded = await self._download_one(
            gallery,
            request,
            complete_on_success=complete_on_success,
        )
        gb = {gallery.gid: downloaded}
        if downloaded or skip_check:
            gb = _merge_results(
                gb,
                await self._download_related_galleries(gallery, policy),
            )
        return gb

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
                    gb, await self.download_by_tag(tag, policy.conditions)
                )
        return gb

    async def deep_download_by_gid(
        self,
        gid: int,
        policy: TagCascadePolicy,
        skip_check: bool = False,
    ) -> dict[int, bool]:
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
        """Process one live snapshot of h2hdb's durable download requests."""
        gb = dict[int, bool]()
        for request in self._queue.download_requests():
            if not self._queue.is_current(request):
                continue
            if request.url:
                gallery = GalleryURLParser(url=request.url)
                direct_result = await self._deep_download_by_gallery(
                    gallery,
                    policy,
                    skip_check,
                    request=request,
                )
                if direct_result[gallery.gid]:
                    gb = _merge_results(gb, direct_result)
                else:
                    # Downloading straight from a URL never falls back to a
                    # gid search, so a stale/dead URL would otherwise just
                    # silently fail here without h2hdb ever recording it as
                    # removed or redirected. Retry via gid to get that.
                    gb = _merge_results(
                        gb,
                        await self._resolve_and_download(
                            gallery.gid,
                            policy=policy,
                            skip_check=skip_check,
                            request=request,
                        ),
                    )
            else:
                gb = _merge_results(
                    gb,
                    await self._resolve_and_download(
                        request.gid,
                        policy=policy,
                        skip_check=skip_check,
                        request=request,
                    ),
                )
        return gb
