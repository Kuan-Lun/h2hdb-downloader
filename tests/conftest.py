import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self, cast

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import DownloadRequest, H2HDBConfig
from hbrowser import (
    GalleryLookupResult,
    GallerySearchResult,
    SearchRequest,
    Tag,
)

from h2hdb_downloader._queue import GalleryQueue
from h2hdb_downloader.downloader import Downloader


@dataclass(frozen=True, slots=True)
class FakeDownloadTurn:
    generation: int
    owner_token: str


@dataclass(frozen=True, slots=True)
class FakeGalleryIngestState:
    completed_generation: int


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
    download_turn_available: bool = True
    next_download_turn_generation: int = 1
    active_download_turn: FakeDownloadTurn | None = None
    handed_off_turn: FakeDownloadTurn | None = None
    completed_ingest_generation: int = 0
    auto_complete_gallery_ingest: bool = True
    claim_download_turn_calls: list[int] = field(default_factory=list)
    renew_download_turn_calls: list[tuple[FakeDownloadTurn, int]] = field(
        default_factory=list
    )
    renew_download_turn_result: bool = True
    renew_download_turn_observer: Callable[[], None] | None = None
    gallery_ingest_requests: list[FakeDownloadTurn] = field(default_factory=list)
    finished_download_turns: list[tuple[FakeDownloadTurn, DownloadRequest]] = field(
        default_factory=list
    )
    finished_missing_download_turns: list[
        tuple[FakeDownloadTurn, DownloadRequest, int]
    ] = field(default_factory=list)
    finish_download_turn_observer: Callable[[], None] | None = None
    accepted_gallery_ingest_turns: set[FakeDownloadTurn] = field(default_factory=set)
    gallery_ingest_state_reads: int = 0

    def assert_database_gate(self) -> None:
        assert self.database_gate_depth > 0

    def complete_gallery_ingest(self) -> None:
        turn = self.handed_off_turn
        assert turn is not None
        self.completed_ingest_generation = max(
            self.completed_ingest_generation,
            turn.generation,
        )
        self.active_download_turn = None
        self.handed_off_turn = None
        self.download_turn_available = True


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

    def delete_removed_gallery_gid(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.removed_gids.discard(gid)


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

    def complete_missing_download_request(
        self,
        request: DownloadRequest,
        gid: int,
    ) -> None:
        self.store.assert_database_gate()
        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)
            self.store.removed_gids.add(gid)

    def clear_removed_gallery_gid(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.removed_gids.discard(gid)

    def update_redownload_time_to_now_by_gid(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.redownload_time_updates.append(gid)
        if gid in self.store.pending_download_gids:
            self.store.pending_download_gids.remove(gid)

    def request_gallery_deletion(self, gid: int) -> None:
        self.store.assert_database_gate()
        self.store.todelete_gids.add(gid)

    def claim_download_turn(self, *, lease_seconds: int) -> FakeDownloadTurn | None:
        self.store.assert_database_gate()
        self.store.claim_download_turn_calls.append(lease_seconds)
        if not self.store.download_turn_available:
            return None

        generation = self.store.next_download_turn_generation
        turn = FakeDownloadTurn(generation, f"turn-token-{generation}")
        self.store.next_download_turn_generation += 1
        self.store.download_turn_available = False
        self.store.active_download_turn = turn
        self.store.handed_off_turn = None
        return turn

    def renew_download_turn(
        self, turn: FakeDownloadTurn, *, lease_seconds: int
    ) -> bool:
        self.store.assert_database_gate()
        self.store.renew_download_turn_calls.append((turn, lease_seconds))
        if self.store.renew_download_turn_observer is not None:
            self.store.renew_download_turn_observer()
        return (
            self.store.renew_download_turn_result
            and self.store.active_download_turn == turn
            and self.store.handed_off_turn is None
        )

    def request_gallery_ingest(self, turn: FakeDownloadTurn) -> bool:
        self.store.assert_database_gate()
        self.store.gallery_ingest_requests.append(turn)
        if turn in self.store.accepted_gallery_ingest_turns:
            return True
        if self.store.active_download_turn != turn:
            return False

        self.store.accepted_gallery_ingest_turns.add(turn)
        self.store.handed_off_turn = turn
        if self.store.auto_complete_gallery_ingest:
            self.store.complete_gallery_ingest()
        return True

    def finish_download_turn(
        self,
        turn: FakeDownloadTurn,
        request: DownloadRequest,
    ) -> bool:
        self.store.assert_database_gate()
        self.store.finished_download_turns.append((turn, request))
        if self.store.finish_download_turn_observer is not None:
            self.store.finish_download_turn_observer()
        if turn in self.store.accepted_gallery_ingest_turns:
            return True
        if self.store.active_download_turn != turn:
            return False

        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)
        self.store.accepted_gallery_ingest_turns.add(turn)
        self.store.handed_off_turn = turn
        if self.store.auto_complete_gallery_ingest:
            self.store.complete_gallery_ingest()
        return True

    def finish_missing_download_turn(
        self,
        turn: FakeDownloadTurn,
        request: DownloadRequest,
        gid: int,
    ) -> bool:
        self.store.assert_database_gate()
        self.store.finished_missing_download_turns.append((turn, request, gid))
        if turn in self.store.accepted_gallery_ingest_turns:
            return True
        if self.store.active_download_turn != turn:
            return False

        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)
            self.store.removed_gids.add(gid)
        self.store.accepted_gallery_ingest_turns.add(turn)
        self.store.handed_off_turn = turn
        if self.store.auto_complete_gallery_ingest:
            self.store.complete_gallery_ingest()
        return True

    def get_gallery_ingest_state(self) -> FakeGalleryIngestState:
        self.store.assert_database_gate()
        self.store.gallery_ingest_state_reads += 1
        return FakeGalleryIngestState(self.store.completed_ingest_generation)


class FakeDriver:
    """Stand-in for ``hbrowser.ExHDriver``: scripted responses, recorded calls."""

    def __init__(self, store: FakeDBStore) -> None:
        self.store = store
        self.download_calls: list[GalleryURLParser] = []
        self.lookup_calls: list[int] = []
        self.search_calls: list[SearchRequest] = []
        self.gallery2tag_calls: list[tuple[GalleryURLParser, str]] = []

        self.download_result: bool | Callable[[GalleryURLParser], Awaitable[bool]] = (
            True
        )
        self.lookup_results: dict[int, GalleryLookupResult] = {}
        self.lookup_observer: Callable[[int], None] | None = None
        self.search_results: dict[tuple[str, str], tuple[GalleryURLParser, ...]] = {}
        self.search_observer: Callable[[SearchRequest], None] | None = None
        self.tag_results: dict[str, list[Tag]] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def download(self, gallery: GalleryURLParser) -> bool:
        assert self.store.database_gate_depth == 0
        self.download_calls.append(gallery)
        if callable(self.download_result):
            return await self.download_result(gallery)
        return self.download_result

    async def lookup_gid(self, gid: int) -> GalleryLookupResult:
        assert self.store.database_gate_depth == 0
        self.lookup_calls.append(gid)
        if self.lookup_observer is not None:
            self.lookup_observer(gid)
        try:
            return self.lookup_results[gid]
        except KeyError:
            raise AssertionError(f"Unscripted GID lookup: {gid}") from None

    async def search(self, request: SearchRequest) -> GallerySearchResult:
        assert self.store.database_gate_depth == 0
        self.search_calls.append(request)
        if self.search_observer is not None:
            self.search_observer(request)
        key = (request.scope_url, request.query)
        try:
            galleries = self.search_results[key]
        except KeyError:
            raise AssertionError(f"Unscripted gallery search: {request!r}") from None
        return GallerySearchResult(
            request=request,
            galleries=galleries,
            pages_visited=1,
        )

    async def gallery2tag(self, gallery: GalleryURLParser, filter: str) -> list[Tag]:
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
    real_sleep = asyncio.sleep

    async def instant_sleep(_seconds: float) -> None:
        await real_sleep(0)

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

    def make(
        *,
        wait4client: int = 0,
        retry2download: int = 0,
        turn_poll_seconds: float = 5,
        turn_lease_seconds: int = 300,
        turn_heartbeat_seconds: float = 60,
    ) -> Downloader:
        return Downloader(
            fake_driver,
            config_path="unused.json",
            csv_path=tmp_path / "todownload_gids.csv",
            wait4client=wait4client,
            retry2download=retry2download,
            turn_poll_seconds=turn_poll_seconds,
            turn_lease_seconds=turn_lease_seconds,
            turn_heartbeat_seconds=turn_heartbeat_seconds,
        )

    return make
