from __future__ import annotations

import csv
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from h2hdb import H2HDBConfig

from h2hdb_downloader._queue import GalleryQueue, TodownloadEntry

if TYPE_CHECKING:
    from .conftest import FakeDBStore


def test_creates_csv_with_header_if_missing(
    queue_factory: Callable[..., GalleryQueue], tmp_path: Path
) -> None:
    path = os.path.join(str(tmp_path), "todownload_gids.csv")
    queue_factory(path)
    with open(path, newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows == [["gid", "url"]]


def test_csv_rows_are_absorbed_into_db_and_csv_is_emptied(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore, tmp_path: Path
) -> None:
    path = os.path.join(str(tmp_path), "todownload_gids.csv")
    with open(path, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["gid", "url"])
        writer.writerow(["", "https://exhentai.org/g/123/abcdef0123/"])
        writer.writerow(["456", ""])

    queue_factory(path)

    assert fake_store.todownload == {
        123: "https://exhentai.org/g/123/abcdef0123/",
        456: "",
    }
    with open(path, newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows == [["gid", "url"]]


def test_refresh_reabsorbs_csv_rows_added_after_construction(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore, tmp_path: Path
) -> None:
    path = os.path.join(str(tmp_path), "todownload_gids.csv")
    queue = queue_factory(path)
    assert fake_store.todownload == {}

    with open(path, mode="a", newline="", encoding="utf-8") as file:
        csv.writer(file).writerow(["789", ""])

    queue.refresh()
    assert 789 in fake_store.todownload


def test_residual_todownload_row_from_interrupted_run_survives_construction(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    """A gid left mid-flight by a crashed prior run must not be lost: it's
    visible via todownload_gids() so the caller can resume it, and excluded
    from pass_gids so it won't be skipped as already-settled."""
    fake_store.gids = {1}
    fake_store.todownload = {1: "https://exhentai.org/g/1/abcdef0123/"}

    queue = queue_factory()

    assert TodownloadEntry(1, "https://exhentai.org/g/1/abcdef0123/") in (
        queue.todownload_gids()
    )
    assert 1 not in queue.pass_gids


def test_mark_inflight_then_clear_inflight_round_trips(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()
    queue.mark_inflight(1, "https://exhentai.org/g/1/abcdef0123/")
    assert 1 in fake_store.todownload

    queue.clear_inflight(1)
    assert 1 not in fake_store.todownload


def test_mark_done_settles_gid_and_drops_it_from_pending(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    fake_store.pending_download_gids = [1]
    queue = queue_factory()
    assert 1 in queue.pending_download_gids

    queue.mark_done(1)

    assert 1 in queue.pass_gids
    assert 1 not in queue.pending_download_gids


def test_csv_path_none_disables_manual_queue_without_touching_filesystem(
    patch_h2hdb: FakeDBStore, tmp_path: Path
) -> None:
    queue = GalleryQueue(config=cast(H2HDBConfig, object()), csv_path=None)

    assert queue.todownload_gids() == []
    assert list(os.listdir(tmp_path)) == []

    queue.mark_inflight(1, "https://exhentai.org/g/1/abcdef0123/")
    assert TodownloadEntry(1, "https://exhentai.org/g/1/abcdef0123/") in (
        queue.todownload_gids()
    )


def test_note_attempt_outcome_resets_wocount_on_success_and_increments_on_skip(
    queue_factory: Callable[..., GalleryQueue],
) -> None:
    queue = queue_factory()
    queue.wocount = 5

    queue.note_attempt_outcome(downloaded=False)
    assert queue.wocount == 6

    queue.note_attempt_outcome(downloaded=True)
    assert queue.wocount == 0
