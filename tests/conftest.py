from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import DownloadRequest, H2HDBConfig
from hbrowser import ExHDriver

from h2hdb_downloader._queue import GalleryQueue
from h2hdb_downloader.downloader import Downloader


@dataclass
class FakeDBStore:
    """In-memory stand-in for the parts of the h2hdb schema this package touches."""

    gids: set[int] = field(default_factory=set)
    pending_download_gids: list[int] = field(default_factory=list)
    download_requests: dict[int, DownloadRequest] = field(default_factory=dict)
    next_request_token: int = 1
    removed_gids: set[int] = field(default_factory=set)
    todelete_gids: set[int] = field(default_factory=set)
    redownload_time_updates: list[int] = field(default_factory=list)
    request_download_error: Exception | None = None
    request_download_observer: Callable[[], None] | None = None
    database_gate_depth: int = 0
    database_gate_timeouts: list[int] = field(default_factory=list)
    connector_exit_gate_depths: list[int] = field(default_factory=list)

    def assert_database_gate(self) -> None:
        assert self.database_gate_depth > 0


class FakeGalleryGIDs:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store

    def get_gids(self) -> list[int]:
        self.store.assert_database_gate()
        return list(self.store.gids)

    def check_gid_by_gid(self, gid: int) -> bool:
        self.store.assert_database_gate()
        return gid in self.store.gids


class FakeRemovedGalleries:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store

    def insert_removed_gallery_gid(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.removed_gids.add(gid)


class FakeConnector:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store
        self.gallery_gids = FakeGalleryGIDs(store)
        self.removed_galleries = FakeRemovedGalleries(store)

    def __enter__(self) -> FakeConnector:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.store.connector_exit_gate_depths.append(self.store.database_gate_depth)
        self.store.assert_database_gate()

    @contextmanager
    def database_gate(self, *, timeout_seconds: int) -> Generator[FakeConnector]:
        self.store.database_gate_timeouts.append(timeout_seconds)
        self.store.database_gate_depth += 1
        try:
            yield self
        finally:
            self.store.database_gate_depth -= 1

    def get_pending_download_gids(self) -> list[int]:
        self.store.assert_database_gate()
        return [
            gid
            for gid in self.store.pending_download_gids
            if gid not in self.store.removed_gids
            and gid not in self.store.todelete_gids
        ]

    def get_download_requests(self) -> list[DownloadRequest]:
        self.store.assert_database_gate()
        return sorted(self.store.download_requests.values(), key=lambda item: item.gid)

    def get_download_request(self, gid: int) -> DownloadRequest | None:
        self.store.assert_database_gate()
        return self.store.download_requests.get(gid)

    def request_download(self, gid: int, url: str = "") -> DownloadRequest:
        self.store.assert_database_gate()
        if self.store.request_download_error is not None:
            raise self.store.request_download_error
        if self.store.request_download_observer is not None:
            self.store.request_download_observer()
        if url:
            parsed_gid = GalleryURLParser(url=url).gid
            if gid not in (0, parsed_gid):
                raise ValueError
            gid = parsed_gid
        if gid <= 0:
            raise ValueError
        existing = self.store.download_requests.get(gid)
        if not url and existing is not None:
            url = existing.url
        request = DownloadRequest(gid, url, f"token-{self.store.next_request_token}")
        self.store.next_request_token += 1
        self.store.download_requests[gid] = request
        return request

    def complete_download_request(self, request: DownloadRequest) -> None:
        self.store.assert_database_gate()
        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)

    def update_redownload_time_to_now_by_gid(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.redownload_time_updates.append(gid)
        if gid in self.store.pending_download_gids:
            self.store.pending_download_gids.remove(gid)

    def request_gallery_deletion(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.todelete_gids.add(gid)


class FakeDriver:
    """Stand-in for ``hbrowser.ExHDriver``: scripted responses, recorded calls."""

    def __init__(self, store: FakeDBStore) -> None:
        self.store = store
        self.download_calls: list[GalleryURLParser] = []
        self.search_calls: list[tuple[str, bool]] = []
        self.get_calls: list[str] = []
        self.gallery2tag_calls: list[tuple[GalleryURLParser, str]] = []

        self.download_result: bool | Callable[[GalleryURLParser], Awaitable[bool]] = (
            True
        )
        self.search_results: dict[str, list[GalleryURLParser]] = {}
        self.search_observer: Callable[[str, bool], None] | None = None
        self.tag_results: dict[str, list[object]] = {}

    async def download(self, gallery: GalleryURLParser) -> bool:
        assert self.store.database_gate_depth == 0
        self.download_calls.append(gallery)
        if callable(self.download_result):
            return await self.download_result(gallery)
        return self.download_result

    async def search(self, key: str, isclear: bool) -> list[GalleryURLParser]:
        assert self.store.database_gate_depth == 0
        self.search_calls.append((key, isclear))
        if self.search_observer is not None:
            self.search_observer(key, isclear)
        return self.search_results.get(key, [])

    async def get(self, url: str) -> None:
        assert self.store.database_gate_depth == 0
        self.get_calls.append(url)

    async def gallery2tag(self, gallery: GalleryURLParser, filter: str) -> list[object]:
        assert self.store.database_gate_depth == 0
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
    """Redirect the queue's ``H2HDB`` lookup at the shared fake store."""

    def factory(*, config: object) -> FakeConnector:
        return FakeConnector(fake_store)

    monkeypatch.setattr("h2hdb_downloader._queue.H2HDB", factory)
    return fake_store


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("h2hdb_downloader.downloader.asyncio.sleep", instant_sleep)


@pytest.fixture
def fake_driver(fake_store: FakeDBStore) -> FakeDriver:
    return FakeDriver(fake_store)


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
