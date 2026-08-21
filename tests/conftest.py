import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self, cast

import pytest
from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import (
    DownloadCandidateState,
    DownloadHandoff,
    DownloadIngestUnavailableError,
    DownloadTurn,
    EnsureDownloadRequestReceipt,
    HandoffKind,
    PendingRedownloadCursor,
    PendingRedownloadPage,
    VNextDownloadQueueFacade,
    VNextDownloadRequest,
)
from hbrowser import (
    GalleryLookupResult,
    GallerySearchResult,
    SearchRequest,
    Tag,
)

from h2hdb_downloader._queue import GalleryQueue
from h2hdb_downloader.downloader import Downloader


def fake_token(label: str | int | bytes) -> bytes:
    if isinstance(label, bytes):
        return label
    return hashlib.sha256(str(label).encode()).digest()[:16]


def fake_request(
    gid: int,
    url: str,
    token: str | int | bytes,
) -> VNextDownloadRequest:
    return VNextDownloadRequest(gid, url, fake_token(token), 0)


def fake_turn(
    generation: int,
    token: str | int | None = None,
    lease_expires_at: int = 10_000,
) -> DownloadTurn:
    return DownloadTurn(
        generation,
        fake_token(token if token is not None else f"turn-{generation}"),
        lease_expires_at,
    )


@dataclass
class FakeDBStore:
    """In-memory stand-in for the parts of the h2hdb schema this package touches."""

    gids: set[int] = field(default_factory=set)
    pending_download_gids: list[int] = field(default_factory=list)
    download_requests: dict[int, VNextDownloadRequest] = field(default_factory=dict)
    next_request_token: int = 1
    removed_gids: set[int] = field(default_factory=set)
    todelete_gids: set[int] = field(default_factory=set)
    request_download_error: Exception | None = None
    request_download_observer: Callable[[], None] | None = None
    download_turn_available: bool = True
    next_download_turn_generation: int = 1
    active_download_turn: DownloadTurn | None = None
    handed_off_turn: DownloadTurn | None = None
    handoffs: dict[int, DownloadHandoff] = field(default_factory=dict)
    completed_ingest_generation: int = 0
    active_turn_download_gids: set[int] = field(default_factory=set)
    auto_complete_gallery_ingest: bool = True
    claim_download_turn_calls: list[int] = field(default_factory=list)
    renew_download_turn_calls: list[tuple[DownloadTurn, int]] = field(
        default_factory=list
    )
    renew_download_turn_result: bool = True
    renew_download_turn_observer: Callable[[], None] | None = None
    gallery_ingest_requests: list[DownloadTurn] = field(default_factory=list)
    finished_download_turns: list[tuple[DownloadTurn, VNextDownloadRequest]] = field(
        default_factory=list
    )
    finished_missing_download_turns: list[
        tuple[DownloadTurn, VNextDownloadRequest, int]
    ] = field(default_factory=list)
    completed_download_requests_in_turn: list[
        tuple[DownloadTurn, VNextDownloadRequest]
    ] = field(default_factory=list)
    completed_missing_download_requests_in_turn: list[
        tuple[DownloadTurn, VNextDownloadRequest, int]
    ] = field(default_factory=list)
    finish_download_turn_observer: Callable[[], None] | None = None
    complete_download_request_in_turn_observer: Callable[[], None] | None = None
    gallery_ingest_state_reads: int = 0

    def complete_gallery_ingest(self) -> None:
        turn = self.handed_off_turn
        assert turn is not None
        self.pending_download_gids = [
            gid
            for gid in self.pending_download_gids
            if gid not in self.active_turn_download_gids
        ]
        self.active_turn_download_gids.clear()
        self.completed_ingest_generation = max(
            self.completed_ingest_generation,
            turn.generation,
        )
        self.active_download_turn = None
        self.handed_off_turn = None
        self.download_turn_available = True


class FakeFacade:
    def __init__(self, store: FakeDBStore) -> None:
        self.store = store

    def list_pending_redownloads(
        self,
        *,
        cursor: PendingRedownloadCursor | None = None,
        limit: int = 256,
    ) -> PendingRedownloadPage:
        after_gid = 0 if cursor is None else cursor.gallery_id
        gids = [
            gid
            for gid in self.store.pending_download_gids
            if gid > after_gid
            if gid not in self.store.removed_gids
            and gid not in self.store.todelete_gids
        ]
        scanned = tuple(sorted(gids)[: limit + 1])
        page_gids = scanned[:limit]
        terminal = len(scanned) <= limit
        next_cursor = None
        if not terminal:
            next_cursor = PendingRedownloadCursor(1, 1, 0, 0, page_gids[-1])
        return PendingRedownloadPage(
            catalog_revision=1,
            source_revision=1,
            cutoff_at=0,
            gids=page_gids,
            next_cursor=next_cursor,
            terminal=terminal,
        )

    def list_download_requests(
        self,
        *,
        after_gid: int = 0,
        limit: int = 1_000,
    ) -> tuple[VNextDownloadRequest, ...]:
        return tuple(
            request
            for request in sorted(
                self.store.download_requests.values(), key=lambda item: item.gid
            )
            if request.gid > after_gid
        )[:limit]

    def get_download_request(self, gid: int) -> VNextDownloadRequest | None:
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

    def request_download(self, gid: int, url: str = "") -> VNextDownloadRequest:
        if self.store.request_download_error is not None:
            raise self.store.request_download_error
        if self.store.request_download_observer is not None:
            self.store.request_download_observer()
        if gid <= 0:
            raise ValueError
        existing = self.store.download_requests.get(gid)
        if not url and existing is not None:
            url = existing.url
        request = fake_request(gid, url, self.store.next_request_token)
        self.store.next_request_token += 1
        self.store.download_requests[gid] = request
        return request

    def ensure_download_request(
        self,
        gid: int,
        url: str = "",
    ) -> EnsureDownloadRequestReceipt:
        if self.store.request_download_error is not None:
            raise self.store.request_download_error
        if self.store.request_download_observer is not None:
            self.store.request_download_observer()
        if gid <= 0:
            raise ValueError

        existing = self.store.download_requests.get(gid)
        if existing is not None:
            if not existing.url and url:
                existing = VNextDownloadRequest(
                    existing.gid,
                    url,
                    existing.request_token,
                    existing.requested_at,
                )
                self.store.download_requests[gid] = existing
            return EnsureDownloadRequestReceipt(existing, created=False)

        request = fake_request(gid, url, self.store.next_request_token)
        self.store.next_request_token += 1
        self.store.download_requests[gid] = request
        return EnsureDownloadRequestReceipt(request, created=True)

    def complete_download_request(self, request: VNextDownloadRequest) -> bool:
        current = self.store.download_requests.get(request.gid)
        if current is not None and current.request_token == request.request_token:
            self.store.download_requests.pop(request.gid)
            return True
        return False

    def complete_missing_download_request(
        self,
        request: VNextDownloadRequest,
        gid: int,
    ) -> bool:
        current = self.store.download_requests.get(request.gid)
        if current is not None and current.request_token == request.request_token:
            self.store.download_requests.pop(request.gid)
            self.store.removed_gids.add(gid)
            return True
        return False

    def record_gallery_found(self, *gids: int) -> None:
        self.store.removed_gids.difference_update(gids)

    def request_deletion(self, gid: int, url: str | None = None) -> object:
        del url
        self.store.todelete_gids.add(gid)
        return object()

    def get_gallery_deletion_requests(self) -> list[int]:
        return sorted(self.store.todelete_gids)

    def claim_download_turn(
        self,
        *,
        lease_duration_microseconds: int,
    ) -> DownloadTurn:
        self.store.claim_download_turn_calls.append(lease_duration_microseconds)
        if not self.store.download_turn_available:
            raise DownloadIngestUnavailableError("download turn unavailable")

        generation = self.store.next_download_turn_generation
        turn = fake_turn(generation)
        self.store.next_download_turn_generation += 1
        self.store.download_turn_available = False
        self.store.active_download_turn = turn
        self.store.handed_off_turn = None
        self.store.active_turn_download_gids.clear()
        return turn

    def renew_download_turn(
        self,
        turn: DownloadTurn,
        *,
        lease_duration_microseconds: int,
    ) -> DownloadTurn:
        self.store.renew_download_turn_calls.append((turn, lease_duration_microseconds))
        if self.store.renew_download_turn_observer is not None:
            self.store.renew_download_turn_observer()
        if (
            not self.store.renew_download_turn_result
            or self.store.active_download_turn != turn
            or self.store.handed_off_turn is not None
        ):
            raise DownloadIngestUnavailableError("download turn is stale")
        renewed = DownloadTurn(
            turn.generation,
            turn.owner_token,
            turn.lease_expires_at + lease_duration_microseconds,
        )
        self.store.active_download_turn = renewed
        return renewed

    def handoff_download_turn(self, turn: DownloadTurn) -> DownloadHandoff:
        self.store.gallery_ingest_requests.append(turn)
        existing = self.store.handoffs.get(turn.generation)
        if existing is not None:
            if existing.owner_token != turn.owner_token:
                raise DownloadIngestUnavailableError("download turn is stale")
            return existing
        if self.store.active_download_turn != turn:
            raise DownloadIngestUnavailableError("download turn is stale")

        handoff = DownloadHandoff(
            turn.generation,
            turn.owner_token,
            HandoffKind.DOWNLOADER,
            turn.generation,
        )
        self.store.handoffs[turn.generation] = handoff
        self.store.handed_off_turn = turn
        if self.store.auto_complete_gallery_ingest:
            self.store.complete_gallery_ingest()
        return handoff

    def complete_download_request_in_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
    ) -> bool:
        self.store.completed_download_requests_in_turn.append((turn, request))
        if self.store.complete_download_request_in_turn_observer is not None:
            self.store.complete_download_request_in_turn_observer()
        if (
            self.store.active_download_turn != turn
            or self.store.handed_off_turn is not None
        ):
            raise DownloadIngestUnavailableError("download turn is stale")

        return self.complete_download_request(request)

    def complete_missing_download_request_in_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
        gid: int,
    ) -> bool:
        self.store.completed_missing_download_requests_in_turn.append(
            (turn, request, gid)
        )
        if (
            self.store.active_download_turn != turn
            or self.store.handed_off_turn is not None
        ):
            raise DownloadIngestUnavailableError("download turn is stale")

        return self.complete_missing_download_request(request, gid)

    def finish_download_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
    ) -> DownloadHandoff:
        self.store.finished_download_turns.append((turn, request))
        if self.store.finish_download_turn_observer is not None:
            self.store.finish_download_turn_observer()
        if self.store.active_download_turn != turn:
            raise DownloadIngestUnavailableError("download turn is stale")

        self.complete_download_request(request)
        return self.handoff_download_turn(turn)

    def finish_missing_download_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
        gid: int,
    ) -> DownloadHandoff:
        self.store.finished_missing_download_turns.append((turn, request, gid))
        if self.store.active_download_turn != turn:
            raise DownloadIngestUnavailableError("download turn is stale")

        self.complete_missing_download_request(request, gid)
        return self.handoff_download_turn(turn)

    def is_download_handoff_complete(self, handoff: DownloadHandoff) -> bool:
        self.store.gallery_ingest_state_reads += 1
        if self.store.handoffs.get(handoff.download_generation) != handoff:
            raise RuntimeError("unknown download handoff")
        return self.store.completed_ingest_generation >= handoff.download_generation


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
            downloaded = await self.download_result(gallery)
        else:
            downloaded = self.download_result
        if downloaded and self.store.active_download_turn is not None:
            self.store.active_turn_download_gids.add(gallery.gid)
        return downloaded

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
def fake_facade(fake_store: FakeDBStore) -> FakeFacade:
    return FakeFacade(fake_store)


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
    fake_facade: FakeFacade, tmp_path: Path
) -> Callable[..., GalleryQueue]:
    def make(csv_path: str | Path | None = None) -> GalleryQueue:
        path = csv_path or tmp_path / "todownload_gids.csv"
        return GalleryQueue(
            facade=cast(VNextDownloadQueueFacade, fake_facade),
            csv_path=path,
        )

    return make


@pytest.fixture
def downloader_factory(
    fake_facade: FakeFacade,
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
            facade=cast(VNextDownloadQueueFacade, fake_facade),
            csv_path=tmp_path / "todownload_gids.csv",
            wait4client=wait4client,
            retry2download=retry2download,
            turn_poll_seconds=turn_poll_seconds,
            turn_lease_seconds=turn_lease_seconds,
            turn_heartbeat_seconds=turn_heartbeat_seconds,
            download_submissions_per_ingest=download_submissions_per_ingest,
        )

    return make
