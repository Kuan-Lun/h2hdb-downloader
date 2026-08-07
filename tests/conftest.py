import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import (
    DownloadCandidateState,
    DownloadRequest,
    DownloadTurn,
    EnsureDownloadRequestResult,
    GalleryIngestPhase,
    GalleryIngestState,
    GalleryIngestTurn,
)
from hbrowser import (
    GalleryLookupResult,
    GallerySearchResult,
    SearchRequest,
    Tag,
)

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
    accepted_submissions: list[tuple[int, DownloadRequest | None]] = field(
        default_factory=list
    )
    redownload_time_updates: list[int] = field(default_factory=list)
    request_download_error: Exception | None = None
    request_download_observer: Callable[[], None] | None = None
    download_turn_available: bool = True
    next_download_turn_generation: int = 1
    active_download_turn: DownloadTurn | None = None
    handed_off_turn: DownloadTurn | None = None
    completed_ingest_generation: int = 0
    auto_complete_gallery_ingest: bool = True
    claim_download_turn_calls: list[int] = field(default_factory=list)
    renew_download_turn_calls: list[tuple[DownloadTurn, int]] = field(
        default_factory=list
    )
    renew_download_turn_result: bool = True
    renew_download_turn_observer: Callable[[], None] | None = None
    gallery_ingest_requests: list[DownloadTurn] = field(default_factory=list)
    finished_download_turns: list[tuple[DownloadTurn, DownloadRequest]] = field(
        default_factory=list
    )
    finished_missing_download_turns: list[tuple[DownloadTurn, DownloadRequest, int]] = (
        field(default_factory=list)
    )
    completed_download_requests_in_turn: list[tuple[DownloadTurn, DownloadRequest]] = (
        field(default_factory=list)
    )
    completed_missing_download_requests_in_turn: list[
        tuple[DownloadTurn, DownloadRequest, int]
    ] = field(default_factory=list)
    finish_download_turn_observer: Callable[[], None] | None = None
    complete_download_request_in_turn_observer: Callable[[], None] | None = None
    accepted_gallery_ingest_turns: set[DownloadTurn] = field(default_factory=set)
    gallery_ingest_state_reads: int = 0

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


class FakeCoordinator:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store

    def get_pending_redownload_gids(self) -> list[int]:
        return [
            gid
            for gid in self.store.pending_download_gids
            if gid not in self.store.removed_gids
            and gid not in self.store.todelete_gids
        ]

    def get_download_requests(self) -> list[DownloadRequest]:
        return sorted(self.store.download_requests.values(), key=lambda item: item.gid)

    def get_download_request(self, gid: int) -> DownloadRequest | None:
        return self.store.download_requests.get(gid)

    def get_candidate_states(
        self, gids: Sequence[int]
    ) -> Mapping[int, DownloadCandidateState]:
        return {
            gid: DownloadCandidateState(
                gid=gid,
                cataloged=gid in self.store.gids,
                redownload_required=gid in self.store.pending_download_gids,
                requested=gid in self.store.download_requests,
            )
            for gid in gids
        }

    def request_download(self, gid: int, url: str = "") -> DownloadRequest:
        if self.store.request_download_error is not None:
            raise self.store.request_download_error
        if self.store.request_download_observer is not None:
            self.store.request_download_observer()
        if gid <= 0:
            raise ValueError
        existing = self.store.download_requests.get(gid)
        if not url and existing is not None:
            url = existing.url
        request = DownloadRequest(gid, url, f"token-{self.store.next_request_token}")
        self.store.next_request_token += 1
        self.store.download_requests[gid] = request
        return request

    def ensure_download_request(
        self,
        gid: int,
        url: str = "",
    ) -> EnsureDownloadRequestResult:
        if self.store.request_download_error is not None:
            raise self.store.request_download_error
        if self.store.request_download_observer is not None:
            self.store.request_download_observer()
        if gid <= 0:
            raise ValueError

        existing = self.store.download_requests.get(gid)
        if existing is not None:
            return EnsureDownloadRequestResult(existing, created=False)

        request = DownloadRequest(gid, url, f"token-{self.store.next_request_token}")
        self.store.next_request_token += 1
        self.store.download_requests[gid] = request
        return EnsureDownloadRequestResult(request, created=True)

    def complete_download_request(self, request: DownloadRequest) -> None:
        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)

    def complete_missing_download_request(
        self,
        request: DownloadRequest,
        gid: int,
    ) -> None:
        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)
            self.store.removed_gids.add(gid)

    def record_gallery_found(self, *gids: int) -> None:
        self.store.removed_gids.difference_update(gids)

    def record_accepted_submission(
        self,
        gid: int,
        *,
        request: DownloadRequest | None = None,
    ) -> None:
        self.store.accepted_submissions.append((gid, request))
        self.store.removed_gids.discard(gid)
        if gid in self.store.gids:
            self.store.redownload_time_updates.append(gid)
        if gid in self.store.pending_download_gids:
            self.store.pending_download_gids.remove(gid)
        if request is not None:
            self.complete_download_request(request)

    def request_gallery_deletion(self, gid: int) -> None:
        self.store.todelete_gids.add(gid)

    def get_gallery_deletion_requests(self) -> list[int]:
        return sorted(self.store.todelete_gids)

    def claim_download_turn(self, *, lease_seconds: int) -> DownloadTurn | None:
        self.store.claim_download_turn_calls.append(lease_seconds)
        if not self.store.download_turn_available:
            return None

        generation = self.store.next_download_turn_generation
        turn = DownloadTurn(generation, f"turn-token-{generation}", 10_000)
        self.store.next_download_turn_generation += 1
        self.store.download_turn_available = False
        self.store.active_download_turn = turn
        self.store.handed_off_turn = None
        return turn

    def renew_download_turn(self, turn: DownloadTurn, *, lease_seconds: int) -> bool:
        self.store.renew_download_turn_calls.append((turn, lease_seconds))
        if self.store.renew_download_turn_observer is not None:
            self.store.renew_download_turn_observer()
        return (
            self.store.renew_download_turn_result
            and self.store.active_download_turn == turn
            and self.store.handed_off_turn is None
        )

    def request_gallery_ingest(self, turn: DownloadTurn) -> bool:
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

    def complete_download_request_in_turn(
        self,
        turn: DownloadTurn,
        request: DownloadRequest,
    ) -> bool:
        self.store.completed_download_requests_in_turn.append((turn, request))
        if self.store.complete_download_request_in_turn_observer is not None:
            self.store.complete_download_request_in_turn_observer()
        if (
            self.store.active_download_turn != turn
            or self.store.handed_off_turn is not None
        ):
            return False

        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)
        return True

    def complete_missing_download_request_in_turn(
        self,
        turn: DownloadTurn,
        request: DownloadRequest,
        gid: int,
    ) -> bool:
        self.store.completed_missing_download_requests_in_turn.append(
            (turn, request, gid)
        )
        if (
            self.store.active_download_turn != turn
            or self.store.handed_off_turn is not None
        ):
            return False

        current = self.store.download_requests.get(request.gid)
        if current is not None and current.token == request.token:
            self.store.download_requests.pop(request.gid)
            self.store.removed_gids.add(gid)
        return True

    def finish_download_turn(
        self,
        turn: DownloadTurn,
        request: DownloadRequest,
    ) -> bool:
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
        turn: DownloadTurn,
        request: DownloadRequest,
        gid: int,
    ) -> bool:
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

    def get_gallery_ingest_state(self) -> GalleryIngestState:
        self.store.gallery_ingest_state_reads += 1
        generation = self.store.next_download_turn_generation - 1
        return GalleryIngestState(
            phase=GalleryIngestPhase.ready,
            generation=generation,
            completed_generation=self.store.completed_ingest_generation,
            owner_token=None,
            lease_expires_at=None,
            handoff_generation=None,
            handoff_owner_token=None,
            last_transition_at=0,
        )

    def claim_gallery_ingest(
        self,
        *,
        lease_seconds: int,
        periodic_scan: bool,
    ) -> GalleryIngestTurn | None:
        raise AssertionError("Downloader must not claim an ingest turn")

    def renew_gallery_ingest(
        self,
        turn: GalleryIngestTurn,
        *,
        lease_seconds: int,
        sqlite_busy_timeout_ms: int | None = None,
    ) -> int | None:
        raise AssertionError("Downloader must not renew an ingest turn")

    def complete_gallery_ingest(
        self,
        turn: GalleryIngestTurn,
        *,
        allow_expired_sqlite_lease: bool = False,
    ) -> bool:
        raise AssertionError("Downloader must not complete an ingest turn")


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
        self.gallery_tag_results: dict[tuple[int, str], list[Tag]] = {}

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
        self.download_calls.append(gallery)
        if callable(self.download_result):
            return await self.download_result(gallery)
        return self.download_result

    async def lookup_gid(self, gid: int) -> GalleryLookupResult:
        self.lookup_calls.append(gid)
        if self.lookup_observer is not None:
            self.lookup_observer(gid)
        try:
            return self.lookup_results[gid]
        except KeyError:
            raise AssertionError(f"Unscripted GID lookup: {gid}") from None

    async def search(self, request: SearchRequest) -> GallerySearchResult:
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
        self.gallery2tag_calls.append((gallery, filter))
        gallery_result = self.gallery_tag_results.get((gallery.gid, filter))
        if gallery_result is not None:
            return gallery_result
        return self.tag_results.get(filter, [])


def gallery(gid: int) -> GalleryURLParser:
    return GalleryURLParser(url=f"https://exhentai.org/g/{gid}/deadbeef00/")


@pytest.fixture
def fake_store() -> FakeDBStore:
    return FakeDBStore()


@pytest.fixture
def fake_coordinator(fake_store: FakeDBStore) -> FakeCoordinator:
    return FakeCoordinator(fake_store)


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
    fake_coordinator: FakeCoordinator, tmp_path: Path
) -> Callable[..., GalleryQueue]:
    def make(csv_path: str | Path | None = None) -> GalleryQueue:
        path = csv_path or tmp_path / "todownload_gids.csv"
        return GalleryQueue(coordinator=fake_coordinator, csv_path=path)

    return make


@pytest.fixture
def downloader_factory(
    fake_coordinator: FakeCoordinator,
    fake_driver: FakeDriver,
    tmp_path: Path,
) -> Callable[..., Downloader]:
    def make(
        *,
        wait4client: int = 0,
        retry2download: int = 0,
        turn_poll_seconds: float = 5,
        turn_lease_seconds: int = 300,
        turn_heartbeat_seconds: float = 60,
        download_submissions_per_ingest: int = 100,
    ) -> Downloader:
        return Downloader(
            fake_driver,
            coordinator=fake_coordinator,
            csv_path=tmp_path / "todownload_gids.csv",
            wait4client=wait4client,
            retry2download=retry2download,
            turn_poll_seconds=turn_poll_seconds,
            turn_lease_seconds=turn_lease_seconds,
            turn_heartbeat_seconds=turn_heartbeat_seconds,
            download_submissions_per_ingest=download_submissions_per_ingest,
        )

    return make
