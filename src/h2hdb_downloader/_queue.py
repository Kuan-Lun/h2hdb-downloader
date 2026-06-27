"""Internal collaborator: durable download queue and dedup tracking.

Not part of the public API. ``Downloader`` is the only public export of this
package; everything here exists to support it.

The underlying database tracks three independent things that this module
ties together:

- an in-flight work log (``todownload_gids``) that ``Downloader`` writes to
  *before* attempting a download and clears *after*, so an interrupted
  process can resume exactly where it left off;
- a completion cache (``pass_gids``) answering "do we already know this gid
  is downloaded and not flagged for redownload?", used to skip redundant
  network calls;
- an optional CSV file that lets an operator queue gids/urls for download
  without touching the database directly, re-absorbed periodically so a
  long-running session can pick up new requests without restarting.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from random import random
from typing import Protocol

from h2hdb import H2HDB, H2HDBConfig

__all__: list[str] = []


@dataclass(frozen=True, slots=True)
class TodownloadEntry:
    """One row of the durable download queue: a gid, optionally with its url."""

    gid: int
    url: str


class _TodownloadGidsReader(Protocol):
    def get_todownload_gids(self) -> list[tuple[int, str]]: ...


def parse_todownload_csv_rows(rows: list[list[str]]) -> list[TodownloadEntry]:
    """Parse data rows (header already excluded) into queue entries.

    A blank gid column means "only the URL is known yet"; it is recorded as
    gid 0 and resolved later once the gallery is actually looked up.
    """
    entries = []
    for row in rows:
        gid = 0 if row[0] == "" else int(row[0])
        entries.append(TodownloadEntry(gid, row[1]))
    return entries


def compute_pass_gids(
    downloaded_gids: list[int],
    pending_download_gids: list[int],
    todownload_gids: list[TodownloadEntry],
) -> list[int]:
    """Gids considered settled: downloaded, and neither queued nor flagged for redownload."""
    return list(
        set(downloaded_gids)
        - set(pending_download_gids)
        - {entry.gid for entry in todownload_gids}
    )


def random_wocount_max() -> int:
    """Pick how many consecutive skips are allowed before forcing a re-verify download."""
    return int(19 * random()) + 1


def should_attempt_download(
    gid: int, pass_gids: set[int], wocount: int, wocount_max: int
) -> bool:
    """Whether a real network download should be attempted for ``gid``.

    Settled gids are skipped to avoid redundant downloads, except every
    ``wocount_max``-th skip in a row, which forces a re-verify download.
    """
    return (gid not in pass_gids) or (wocount > wocount_max)


def read_todownload_csv(path: Path) -> list[TodownloadEntry]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    return parse_todownload_csv_rows(rows[1:])


def write_empty_todownload_csv(path: Path) -> None:
    with path.open(mode="w", newline="", encoding="utf-8") as file:
        csv.writer(file).writerow(["gid", "url"])


def ensure_todownload_csv(path: Path) -> None:
    if not path.exists():
        write_empty_todownload_csv(path)


class GalleryQueue:
    """Owns the durable queue/dedup state backing a single ``Downloader``."""

    def __init__(
        self, config: H2HDBConfig, csv_path: str | os.PathLike[str] | None
    ) -> None:
        """``csv_path=None`` disables the manual CSV queue entirely; the
        durable in-flight log and dedup cache work the same either way."""
        self.config = config
        self.csv_path = Path(csv_path) if csv_path is not None else None
        self.wocount = 0
        self.wocount_max = random_wocount_max()
        self.refresh()

    def refresh(self) -> None:
        """Re-absorb the CSV queue into the database, then reload cached state."""
        self._sync_csv_into_db()
        with H2HDB(config=self.config) as connector:
            downloaded_gids = connector.get_gids()
            self.pending_download_gids = connector.get_pending_download_gids()
            todownload_gids = self._fetch_todownload_gids(connector)
        self.pass_gids = set(
            compute_pass_gids(
                downloaded_gids, self.pending_download_gids, todownload_gids
            )
        )

    def _sync_csv_into_db(self) -> None:
        if self.csv_path is None:
            return
        ensure_todownload_csv(self.csv_path)
        entries = read_todownload_csv(self.csv_path)
        if entries:
            with H2HDB(config=self.config) as connector:
                for entry in entries:
                    connector.insert_todownload_gid(entry.gid, entry.url)
        write_empty_todownload_csv(self.csv_path)

    @staticmethod
    def _fetch_todownload_gids(
        connector: _TodownloadGidsReader,
    ) -> list[TodownloadEntry]:
        return [
            TodownloadEntry(gid, url) for gid, url in connector.get_todownload_gids()
        ]

    def mark_inflight(self, gid: int, url: str) -> None:
        with H2HDB(config=self.config) as connector:
            connector.insert_todownload_gid(gid, url)

    def clear_inflight(self, gid: int) -> None:
        with H2HDB(config=self.config) as connector:
            connector.remove_todownload_gid(gid)

    def todownload_gids(self) -> list[TodownloadEntry]:
        with H2HDB(config=self.config) as connector:
            return self._fetch_todownload_gids(connector)

    def should_attempt(self, gid: int) -> bool:
        return should_attempt_download(
            gid, self.pass_gids, self.wocount, self.wocount_max
        )

    def note_attempt_outcome(self, downloaded: bool) -> None:
        if downloaded:
            self.wocount = 0
            self.wocount_max = random_wocount_max()
        else:
            self.wocount += 1

    def mark_done(self, gid: int) -> None:
        self.pass_gids.add(gid)
        if gid in self.pending_download_gids:
            self.pending_download_gids.remove(gid)
