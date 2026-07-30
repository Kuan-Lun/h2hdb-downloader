"""Internal collaborator: durable download requests and dedup tracking.

Not part of the public API. Everything here exists to support ``Downloader``.

The underlying database tracks four independent things that this module
ties together:

- durable download requests (``todownload_gids``), each identified by an
  immutable token so completing an old attempt cannot erase a newer request;
- the generation-fenced download/ingest turn, including its recoverable lease;
- live h2hdb state answering whether a gid is already settled, used to skip
  redundant network calls without keeping a stale process-local cache;
- an optional CSV file that lets an operator queue gids/urls for download
  without touching the database directly, re-absorbed periodically so a
  long-running session can pick up new requests without restarting.
"""

import csv
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from random import random
from time import time_ns
from typing import Protocol, cast
from uuid import uuid4

from h2hdb import H2HDB, DownloadRequest, H2HDBConfig

__all__: list[str] = []

CSV_HEADER = ["gid", "url"]


@dataclass(frozen=True, slots=True)
class ManualDownloadRequest:
    """One parsed CSV request, before h2hdb assigns its durable token."""

    gid: int
    url: str


class _DownloadRequestsReader(Protocol):
    def get_download_requests(self) -> list[DownloadRequest]: ...


class DownloadTurn(Protocol):
    """The stable portion of h2hdb's download-turn value used here."""

    @property
    def generation(self) -> int: ...

    @property
    def owner_token(self) -> str: ...


class _GalleryIngestState(Protocol):
    @property
    def completed_generation(self) -> int: ...


class _DownloadTurnCoordinator(Protocol):
    def claim_download_turn(self, *, lease_seconds: int) -> DownloadTurn | None: ...

    def renew_download_turn(
        self, turn: DownloadTurn, *, lease_seconds: int
    ) -> bool: ...

    def request_gallery_ingest(self, turn: DownloadTurn) -> bool: ...

    def finish_download_turn(
        self, turn: DownloadTurn, request: DownloadRequest
    ) -> bool: ...

    def get_gallery_ingest_state(self) -> _GalleryIngestState: ...


def parse_todownload_csv_rows(rows: list[list[str]]) -> list[ManualDownloadRequest]:
    """Parse data rows (header already excluded) into queue entries.

    A blank gid column means "only the URL is known yet"; it is recorded as
    gid 0 and resolved later once the gallery is actually looked up.
    """
    entries = []
    for row in rows:
        gid = 0 if row[0] == "" else int(row[0])
        entries.append(ManualDownloadRequest(gid, row[1]))
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
        self, config: H2HDBConfig, csv_path: str | os.PathLike[str] | None
    ) -> None:
        """``csv_path=None`` disables the manual CSV queue."""
        self.config = config
        self.csv_path = Path(csv_path) if csv_path is not None else None
        self.wocount = 0
        self.wocount_max = random_wocount_max()
        self._sync_csv_into_db()

    @contextmanager
    def _database_operation(self) -> Generator[H2HDB]:
        """Yield h2hdb while its cross-process maintenance gate is held."""

        connector = H2HDB(config=self.config)
        with connector.database_gate(timeout_seconds=300):
            with connector:
                yield connector

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
        if entries:
            with self._database_operation() as connector:
                for entry in entries:
                    connector.request_download(entry.gid, entry.url)

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

    @staticmethod
    def _fetch_download_requests(
        connector: _DownloadRequestsReader,
    ) -> list[DownloadRequest]:
        return connector.get_download_requests()

    def request_download(self, gid: int, url: str = "") -> DownloadRequest:
        with self._database_operation() as connector:
            return connector.request_download(gid, url)

    def complete_download_request(self, request: DownloadRequest) -> None:
        with self._database_operation() as connector:
            connector.complete_download_request(request)

    def claim_download_turn(self, *, lease_seconds: int) -> DownloadTurn | None:
        with self._database_operation() as connector:
            coordinator = cast(_DownloadTurnCoordinator, connector)
            return coordinator.claim_download_turn(lease_seconds=lease_seconds)

    def renew_download_turn(self, turn: DownloadTurn, *, lease_seconds: int) -> bool:
        with self._database_operation() as connector:
            coordinator = cast(_DownloadTurnCoordinator, connector)
            return coordinator.renew_download_turn(
                turn,
                lease_seconds=lease_seconds,
            )

    def request_gallery_ingest(self, turn: DownloadTurn) -> bool:
        with self._database_operation() as connector:
            coordinator = cast(_DownloadTurnCoordinator, connector)
            return coordinator.request_gallery_ingest(turn)

    def finish_download_turn(
        self, turn: DownloadTurn, request: DownloadRequest
    ) -> bool:
        with self._database_operation() as connector:
            coordinator = cast(_DownloadTurnCoordinator, connector)
            return coordinator.finish_download_turn(turn, request)

    def completed_gallery_ingest_generation(self) -> int:
        with self._database_operation() as connector:
            coordinator = cast(_DownloadTurnCoordinator, connector)
            return coordinator.get_gallery_ingest_state().completed_generation

    def is_current(self, request: DownloadRequest) -> bool:
        with self._database_operation() as connector:
            current = connector.get_download_request(request.gid)
        return (
            current is not None
            and current.gid == request.gid
            and current.token == request.token
        )

    def download_requests(self) -> list[DownloadRequest]:
        """Absorb manual CSV work, then return a live database snapshot."""
        self._sync_csv_into_db()
        with self._database_operation() as connector:
            return self._fetch_download_requests(connector)

    def should_attempt(self, gid: int) -> bool:
        with self._database_operation() as connector:
            is_downloaded = connector.gallery_gids.check_gid_by_gid(gid)
            is_pending = gid in connector.get_pending_download_gids()
            is_requested = connector.get_download_request(gid) is not None
        return should_attempt_download(
            is_downloaded=is_downloaded,
            is_pending=is_pending,
            is_requested=is_requested,
            wocount=self.wocount,
            wocount_max=self.wocount_max,
        )

    def pending_redownload_gids(self) -> list[int]:
        with self._database_operation() as connector:
            return connector.get_pending_download_gids()

    def note_skip(self) -> None:
        self.wocount += 1

    def note_download_success(self) -> None:
        self.wocount = 0
        self.wocount_max = random_wocount_max()
