from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import H2HDBConfig
from hbrowser import ExHDriver

from h2hdb_downloader._queue import GalleryQueue
from h2hdb_downloader.downloader import Downloader


@dataclass
class FakeDBStore:
    """In-memory stand-in for the parts of the h2hdb schema this package touches."""

    gids: set[int] = field(default_factory=set)
    pending_download_gids: list[int] = field(default_factory=list)
    todownload: dict[int, str] = field(default_factory=dict)
    removed_gids: set[int] = field(default_factory=set)
    todelete_gids: set[int] = field(default_factory=set)
    redownload_time_updates: list[int] = field(default_factory=list)


class FakeGalleryGIDs:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store

    def get_gids(self) -> list[int]:
        return list(self.store.gids)

    def check_gid_by_gid(self, gid: int) -> bool:
        return gid in self.store.gids


class FakeRemovedGalleries:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store

    def insert_removed_gallery_gid(self, gid: int) -> None:
        self.store.removed_gids.add(gid)


class FakeConnector:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store
        self.gallery_gids = FakeGalleryGIDs(store)
        self.removed_galleries = FakeRemovedGalleries(store)

    def __enter__(self) -> FakeConnector:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get_pending_download_gids(self) -> list[int]:
        return list(self.store.pending_download_gids)

    def get_todownload_gids(self) -> list[tuple[int, str]]:
        return list(self.store.todownload.items())

    def insert_todownload_gid(self, gid: int, url: str) -> None:
        if url:
            gid = GalleryURLParser(url=url).gid
        self.store.todownload[gid] = url

    def remove_todownload_gid(self, gid: int) -> None:
        self.store.todownload.pop(gid, None)

    def update_redownload_time_to_now_by_gid(self, gid: int) -> None:
        self.store.redownload_time_updates.append(gid)

    def insert_todelete_gid(self, gid: int) -> None:
        self.store.todelete_gids.add(gid)


class FakeDriver:
    """Stand-in for ``hbrowser.ExHDriver``: scripted responses, recorded calls."""

    def __init__(self) -> None:
        self.download_calls: list[GalleryURLParser] = []
        self.search_calls: list[tuple[str, bool]] = []
        self.get_calls: list[str] = []
        self.gallery2tag_calls: list[tuple[GalleryURLParser, str]] = []

        self.download_result: bool | Callable[[GalleryURLParser], Awaitable[bool]] = (
            True
        )
        self.search_results: dict[str, list[GalleryURLParser]] = {}
        self.tag_results: dict[str, list[object]] = {}

    async def download(self, gallery: GalleryURLParser) -> bool:
        self.download_calls.append(gallery)
        if callable(self.download_result):
            return await self.download_result(gallery)
        return self.download_result

    async def search(self, key: str, isclear: bool) -> list[GalleryURLParser]:
        self.search_calls.append((key, isclear))
        return self.search_results.get(key, [])

    async def get(self, url: str) -> None:
        self.get_calls.append(url)

    async def gallery2tag(self, gallery: GalleryURLParser, filter: str) -> list[object]:
        self.gallery2tag_calls.append((gallery, filter))
        return self.tag_results.get(filter, [])


def gallery(gid: int) -> GalleryURLParser:
    return GalleryURLParser(url=f"https://exhentai.org/g/{gid}/deadbeef00/")


@pytest.fixture
def fake_store() -> FakeDBStore:
    return FakeDBStore()


@pytest.fixture
def patch_h2hdb(
    monkeypatch: pytest.MonkeyPatch, fake_store: FakeDBStore
) -> FakeDBStore:
    """Redirect both modules' ``H2HDB`` lookups at the shared fake store."""

    def factory(*, config: object) -> FakeConnector:
        return FakeConnector(fake_store)

    monkeypatch.setattr("h2hdb_downloader._queue.H2HDB", factory)
    monkeypatch.setattr("h2hdb_downloader.downloader.H2HDB", factory)
    return fake_store


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("h2hdb_downloader.downloader.asyncio.sleep", instant_sleep)


@pytest.fixture
def fake_driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def queue_factory(
    patch_h2hdb: FakeDBStore, tmp_path: Path
) -> Callable[..., GalleryQueue]:
    def make(csv_path: str | Path | None = None) -> GalleryQueue:
        path = csv_path or tmp_path / "todownload_gids.csv"
        return GalleryQueue(config=cast(H2HDBConfig, object()), csv_path=path)

    return make


@pytest.fixture
def downloader_factory(
    monkeypatch: pytest.MonkeyPatch,
    patch_h2hdb: FakeDBStore,
    fake_driver: FakeDriver,
    tmp_path: Path,
) -> Callable[..., Downloader]:
    monkeypatch.setattr(
        "h2hdb_downloader.downloader.load_config", lambda config_path: object()
    )

    def make(*, wait4client: int = 0, retry2download: int = 0) -> Downloader:
        return Downloader(
            cast(ExHDriver, fake_driver),
            config_path="unused.json",
            csv_path=tmp_path / "todownload_gids.csv",
            wait4client=wait4client,
            retry2download=retry2download,
        )

    return make
