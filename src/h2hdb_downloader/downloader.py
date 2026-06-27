import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from random import random

from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import H2HDB, load_config
from hbrowser import ExHDriver, Tag
from hbrowser.exceptions import ClientOfflineException, InsufficientFundsException

from ._queue import GalleryQueue

type _DownloadFn = Callable[..., Awaitable[dict[GalleryURLParser, bool]]]


def _merge_results(
    dict1: dict[GalleryURLParser, bool], dict2: dict[GalleryURLParser, bool]
) -> dict[GalleryURLParser, bool]:
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
    download queue internally (see ``_queue.GalleryQueue``) so an interrupted
    run can resume without losing track of in-flight work. ``csv_path`` is
    optional: it only enables the manual "queue a gid/url by editing a CSV
    file" feature — leave it as ``None`` if you don't need that.

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
    ) -> dict[GalleryURLParser, bool]:
        """Download one known gallery, or several, with retry on transient errors."""
        if isinstance(target, GalleryURLParser):
            return {target: await self._download_one(target)}
        gb = dict[GalleryURLParser, bool]()
        for gallery in target:
            gb[gallery] = await self._download_one(gallery)
        return gb

    async def _download_one(self, gallery: GalleryURLParser) -> bool:
        async def raise_after_wait(wait_seconds: int, error: Exception) -> None:
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            else:
                raise error

        try:
            return await self._attempt_download(gallery)
        except ClientOfflineException as e:
            await raise_after_wait(self.wait4client, e)
            return await self._attempt_download(gallery)
        except InsufficientFundsException as e:
            await raise_after_wait(self.retry2download, e)
            return await self._attempt_download(gallery)

    async def _attempt_download(self, gallery: GalleryURLParser) -> bool:
        self._queue.mark_inflight(gallery.gid, gallery.url)
        try:
            if self._queue.should_attempt(gallery.gid):
                downloaded = await self.driver.download(gallery)
            else:
                downloaded = False
            if downloaded:
                with H2HDB(config=self._queue.config) as connector:
                    if connector.check_gid_by_gid(gallery.gid):
                        connector.update_redownload_time_to_now_by_gid(gallery.gid)
                self._queue.mark_done(gallery.gid)
                await asyncio.sleep(random())
            self._queue.note_attempt_outcome(downloaded)
            return downloaded
        finally:
            self._queue.clear_inflight(gallery.gid)

    async def download_by_gid(self, gid: int) -> dict[GalleryURLParser, bool]:
        """Resolve a bare gid to its gallery (via search) and download it.

        Always settles ``gid`` in the pending-redownload queue before
        returning, whether it was downloaded, no longer exists, or
        redirected to a different gid — callers never need to do that
        bookkeeping themselves.
        """
        return await self._resolve_and_download(gid, self.download_by_gallery)

    async def _resolve_and_download(
        self,
        gid: int,
        download: _DownloadFn,
        **download_kwargs: object,
    ) -> dict[GalleryURLParser, bool]:
        gb = dict[GalleryURLParser, bool]()
        galleries = await self.driver.search(f"gid:{gid}", isclear=True)
        match len(galleries):
            case 0:
                with H2HDB(config=self._queue.config) as connector:
                    connector.insert_removed_gallery_gid(gid)
            case 1:
                gallery = galleries[0]
                gb[gallery] = (await download(gallery, **download_kwargs))[gallery]
                if gallery.gid != gid:
                    with H2HDB(config=self._queue.config) as connector:
                        if connector.check_gid_by_gid(gid):
                            connector.insert_todelete_gid(gid)
            case _:
                raise ValueError("There can only be one gallery or none.")
        self._queue.mark_done(gid)
        return gb

    async def download_by_tag(
        self, tag: Tag, conditions: Sequence[str]
    ) -> dict[GalleryURLParser, bool]:
        """Download every gallery under ``tag`` matching each of ``conditions``."""
        gb = dict[GalleryURLParser, bool]()
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
    ) -> dict[GalleryURLParser, bool]:
        """Download ``gallery``, then cascade into its artist/group tags.

        ``skip_check`` forces the cascade to run even when ``gallery`` itself
        was skipped as already-settled (e.g. because it was just downloaded
        moments ago by a separate call).
        """
        downloaded = await self.download_by_gallery(gallery)
        gb = dict(downloaded)
        if downloaded[gallery] or skip_check:
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
    ) -> dict[GalleryURLParser, bool]:
        return await self._resolve_and_download(
            gid,
            self.deep_download_by_gallery,
            policy=policy,
            skip_check=skip_check,
        )

    def pending_redownload_gids(self) -> list[int]:
        """Gids h2hdb currently flags as needing a redownload, oldest first."""
        return list(self._queue.pending_download_gids)

    async def drain_queue(
        self, policy: TagCascadePolicy, skip_check: bool = True
    ) -> dict[GalleryURLParser, bool]:
        """Process everything currently queued: manually-requested gids/urls
        plus anything left in-flight by a previous, interrupted run."""
        gb = dict[GalleryURLParser, bool]()
        for entry in self._queue.todownload_gids():
            if entry.url:
                gallery = GalleryURLParser(url=entry.url)
                direct_result = await self.deep_download_by_gallery(
                    gallery, policy, skip_check
                )
                if direct_result[gallery]:
                    gb = _merge_results(gb, direct_result)
                else:
                    # Downloading straight from a URL never falls back to a
                    # gid search, so a stale/dead URL would otherwise just
                    # silently fail here without h2hdb ever recording it as
                    # removed or redirected. Retry via gid to get that.
                    gb = _merge_results(
                        gb,
                        await self.deep_download_by_gid(
                            gallery.gid, policy, skip_check
                        ),
                    )
                self._queue.clear_inflight(gallery.gid)
            else:
                gb = _merge_results(
                    gb,
                    await self.deep_download_by_gid(entry.gid, policy, skip_check),
                )
                self._queue.clear_inflight(entry.gid)
        return gb
