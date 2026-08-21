"""Internal collaborator: durable download requests and dedup tracking.

Not part of the public API. Everything here exists to support ``Downloader``.

The underlying database tracks four independent things that this module
ties together:

- normalized durable download requests, each identified by an immutable token
  so completing an old attempt cannot erase a newer request;
- the generation-fenced download/ingest turn, including its recoverable lease;
- live h2hdb state answering whether a gid is already settled, used to skip
  redundant network calls without keeping a stale process-local cache;
- an optional CSV file that lets an operator queue gids/urls for download
  without touching the database directly, re-absorbed periodically so a
  long-running session can pick up new requests without restarting.
"""

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from random import random
from time import time_ns
from uuid import uuid4

from h2h_galleryinfo_parser import GalleryURLParser
from h2hdb import (
    DownloadHandoff,
    DownloadTurn,
    EnsureDownloadRequestReceipt,
    VNextDownloadQueueFacade,
    VNextDownloadRequest,
)

__all__: list[str] = []

CSV_HEADER = ["gid", "url"]
_DOWNLOAD_REQUEST_PAGE_LIMIT = 1_000
_PENDING_REDOWNLOAD_PAGE_LIMIT = 256
_MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(frozen=True, slots=True)
class ManualDownloadRequest:
    """One parsed CSV request, before h2hdb assigns its durable token."""

    gid: int
    url: str


def _validate_request_identity(gid: int, url: str) -> int:
    """Validate the provider-specific URL before it crosses into neutral core."""

    if gid <= 0:
        raise ValueError("Gallery GID must be greater than zero.")
    if not url:
        return gid
    parsed_gid = GalleryURLParser(url).gid
    if parsed_gid != gid:
        raise ValueError(f"Gallery GID {gid} does not match URL GID {parsed_gid}.")
    return gid


def parse_todownload_csv_rows(rows: list[list[str]]) -> list[ManualDownloadRequest]:
    """Parse data rows (header already excluded) into queue entries.

    A blank gid column means "derive the gid from the URL". URL parsing belongs
    to this downloader adapter; the core facade only receives normalized
    positive gids.
    """
    entries = []
    for row in rows:
        if len(row) != 2:
            raise ValueError("Download CSV rows must contain exactly gid and url.")
        url = row[1]
        gid = GalleryURLParser(url).gid if row[0] == "" else int(row[0])
        entries.append(ManualDownloadRequest(_validate_request_identity(gid, url), url))
    return entries


def random_wocount_max() -> int:
    """Pick how many consecutive skips are allowed before forcing a re-verify download."""
    return int(19 * random()) + 1


def should_attempt_download(
    *,
    is_downloaded: bool,
    is_pending: bool,
    is_requested: bool,
    wocount: int,
    wocount_max: int,
) -> bool:
    """Whether a real network download should be attempted.

    A gid is settled only when it is already downloaded and has neither a
    redownload flag nor a durable request. Settled gids are normally skipped,
    except every ``wocount_max``-th skip in a row, which forces a re-verify.
    """
    is_settled = is_downloaded and not is_pending and not is_requested
    return not is_settled or wocount > wocount_max


def read_todownload_csv(path: Path) -> list[ManualDownloadRequest]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    if rows and rows[0] == CSV_HEADER:
        rows = rows[1:]
    return parse_todownload_csv_rows(rows)


def ensure_todownload_csv(path: Path) -> None:
    try:
        with path.open(mode="x", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(CSV_HEADER)
    except FileExistsError:
        pass


def _claim_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.claim-{time_ns():020d}-{uuid4().hex}")


def _existing_claim_paths(path: Path) -> list[Path]:
    prefix = f".{path.name}.claim-"
    claims: list[tuple[int, str, Path]] = []
    for candidate in path.parent.iterdir():
        if not candidate.name.startswith(prefix):
            continue
        try:
            if candidate.is_file():
                claims.append((candidate.stat().st_mtime_ns, candidate.name, candidate))
        except FileNotFoundError:
            continue
    return [candidate for _, _, candidate in sorted(claims)]


def _claim_current_csv(path: Path) -> Path | None:
    """Atomically detach current work while immediately restoring the inbox path."""

    ensure_todownload_csv(path)
    if not read_todownload_csv(path):
        return None

    claim_path = _claim_path(path)
    try:
        os.replace(path, claim_path)
    except FileNotFoundError:
        # Another process may have claimed the inbox after our read. Its claim
        # is independently recoverable; recreate the inbox if necessary.
        ensure_todownload_csv(path)
        return None
    ensure_todownload_csv(path)
    return claim_path


class GalleryQueue:
    """Mediates h2hdb's durable requests and live deduplication state."""

    def __init__(
        self,
        facade: VNextDownloadQueueFacade,
        csv_path: str | os.PathLike[str] | None,
    ) -> None:
        """``csv_path=None`` disables the manual CSV queue."""
        self._facade = facade
        self.csv_path = Path(csv_path) if csv_path is not None else None
        self.wocount = 0
        self.wocount_max = random_wocount_max()
        self._sync_csv_into_db()

    def _sync_csv_into_db(self) -> None:
        if self.csv_path is None:
            return
        ensure_todownload_csv(self.csv_path)
        for claim_path in _existing_claim_paths(self.csv_path):
            if not self._replay_csv_claim(claim_path):
                return

        current_claim_path = _claim_current_csv(self.csv_path)
        if current_claim_path is not None:
            self._replay_csv_claim(current_claim_path)

    def _replay_csv_claim(self, claim_path: Path) -> bool:
        """Replay one crash-recoverable claim and remove it only when stable."""

        try:
            before = claim_path.stat()
        except FileNotFoundError:
            return True

        entries = read_todownload_csv(claim_path)
        for entry in entries:
            self._facade.request_download(entry.gid, entry.url)

        try:
            after = claim_path.stat()
        except FileNotFoundError:
            return True
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            # A writer that had the pre-rotation file open appended to the
            # claimed inode. Keep it so the next sync replays those new rows.
            return False

        claim_path.unlink(missing_ok=True)
        return True

    def request_download(self, gid: int, url: str = "") -> VNextDownloadRequest:
        return self._facade.request_download(
            _validate_request_identity(gid, url),
            url,
        )

    def ensure_download_request(
        self,
        gid: int,
        url: str = "",
    ) -> EnsureDownloadRequestReceipt:
        """Create a request only when no request for ``gid`` already exists."""

        return self._facade.ensure_download_request(
            _validate_request_identity(gid, url),
            url,
        )

    def complete_download_request(self, request: VNextDownloadRequest) -> bool:
        return self._facade.complete_download_request(request)

    def complete_missing_download_request(
        self,
        request: VNextDownloadRequest,
        gid: int,
    ) -> bool:
        return self._facade.complete_missing_download_request(request, gid)

    def record_gallery_found(self, *gids: int) -> None:
        self._facade.record_gallery_found(*gids)

    def request_gallery_deletion(self, gid: int) -> None:
        self._facade.request_deletion(gid)

    def is_cataloged(self, gid: int) -> bool:
        return self._facade.get_candidate_states((gid,))[gid].cataloged

    def claim_download_turn(self, *, lease_seconds: int) -> DownloadTurn:
        return self._facade.claim_download_turn(
            lease_duration_microseconds=lease_seconds * _MICROSECONDS_PER_SECOND
        )

    def renew_download_turn(
        self,
        turn: DownloadTurn,
        *,
        lease_seconds: int,
    ) -> DownloadTurn:
        return self._facade.renew_download_turn(
            turn,
            lease_duration_microseconds=lease_seconds * _MICROSECONDS_PER_SECOND,
        )

    def handoff_download_turn(self, turn: DownloadTurn) -> DownloadHandoff:
        return self._facade.handoff_download_turn(turn)

    def complete_download_request_in_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
    ) -> bool:
        return self._facade.complete_download_request_in_turn(turn, request)

    def complete_missing_download_request_in_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
        gid: int,
    ) -> bool:
        return self._facade.complete_missing_download_request_in_turn(
            turn,
            request,
            gid,
        )

    def finish_download_turn(
        self, turn: DownloadTurn, request: VNextDownloadRequest
    ) -> DownloadHandoff:
        return self._facade.finish_download_turn(turn, request)

    def finish_missing_download_turn(
        self,
        turn: DownloadTurn,
        request: VNextDownloadRequest,
        gid: int,
    ) -> DownloadHandoff:
        return self._facade.finish_missing_download_turn(turn, request, gid)

    def is_download_handoff_complete(self, handoff: DownloadHandoff) -> bool:
        return self._facade.is_download_handoff_complete(handoff)

    def is_current(self, request: VNextDownloadRequest) -> bool:
        current = self._facade.get_download_request(request.gid)
        return (
            current is not None
            and current.gid == request.gid
            and current.request_token == request.request_token
        )

    def download_requests(self) -> list[VNextDownloadRequest]:
        """Absorb manual CSV work, then collect a bounded-page live snapshot."""
        self._sync_csv_into_db()
        requests: list[VNextDownloadRequest] = []
        after_gid = 0
        while page := self._facade.list_download_requests(
            after_gid=after_gid,
            limit=_DOWNLOAD_REQUEST_PAGE_LIMIT,
        ):
            requests.extend(page)
            after_gid = page[-1].gid
        return requests

    def should_attempt(self, gid: int) -> bool:
        state = self._facade.get_candidate_states((gid,))[gid]
        return should_attempt_download(
            is_downloaded=state.cataloged,
            is_pending=state.redownload_required,
            is_requested=state.requested,
            wocount=self.wocount,
            wocount_max=self.wocount_max,
        )

    def pending_redownload_gids(self) -> list[int]:
        gids: list[int] = []
        cursor = None
        while True:
            page = self._facade.list_pending_redownloads(
                cursor=cursor,
                limit=_PENDING_REDOWNLOAD_PAGE_LIMIT,
            )
            gids.extend(page.gids)
            if page.terminal:
                return gids
            cursor = page.next_cursor
            if cursor is None:
                raise RuntimeError(
                    "nonterminal pending-redownload page omitted its cursor"
                )

    def note_skip(self) -> None:
        self.wocount += 1

    def note_download_success(self) -> None:
        self.wocount = 0
        self.wocount_max = random_wocount_max()
