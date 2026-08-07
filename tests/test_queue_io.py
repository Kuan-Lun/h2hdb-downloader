import csv
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from h2hdb import DownloadRequest, DownloadTurn

from h2hdb_downloader._queue import GalleryQueue

if TYPE_CHECKING:
    from .conftest import FakeCoordinator, FakeDBStore


def claim_paths(path: Path) -> list[Path]:
    return sorted(
        candidate
        for candidate in path.parent.iterdir()
        if candidate.name.startswith(f".{path.name}.claim-")
    )


def test_creates_csv_with_header_if_missing(
    queue_factory: Callable[..., GalleryQueue], tmp_path: Path
) -> None:
    path = tmp_path / "todownload_gids.csv"
    queue_factory(path)
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows == [["gid", "url"]]


def test_csv_rows_are_absorbed_into_db_and_csv_is_emptied(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore, tmp_path: Path
) -> None:
    path = tmp_path / "todownload_gids.csv"
    with path.open(mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["gid", "url"])
        writer.writerow(["", "https://exhentai.org/g/123/abcdef0123/"])
        writer.writerow(["456", ""])

    queue_factory(path)

    assert {
        gid: request.url for gid, request in fake_store.download_requests.items()
    } == {123: "https://exhentai.org/g/123/abcdef0123/", 456: ""}
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert rows == [["gid", "url"]]


def test_download_requests_reabsorbs_csv_rows_added_after_construction(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore, tmp_path: Path
) -> None:
    path = tmp_path / "todownload_gids.csv"
    queue = queue_factory(path)
    assert fake_store.download_requests == {}

    with path.open(mode="a", newline="", encoding="utf-8") as file:
        csv.writer(file).writerow(["789", ""])

    requests = queue.download_requests()

    assert [request.gid for request in requests] == [789]
    assert 789 in fake_store.download_requests


def test_database_failure_preserves_claim_for_next_startup(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
    tmp_path: Path,
) -> None:
    path = tmp_path / "todownload_gids.csv"
    with path.open(mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["gid", "url"])
        writer.writerow(["123", ""])
    fake_store.request_download_error = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        queue_factory(path)

    assert len(claim_paths(path)) == 1
    assert fake_store.download_requests == {}

    fake_store.request_download_error = None
    queue_factory(path)

    assert set(fake_store.download_requests) == {123}
    assert claim_paths(path) == []


def test_append_during_atomic_rotation_remains_in_inbox(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "todownload_gids.csv"
    with path.open(mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["gid", "url"])
        writer.writerow(["123", ""])

    original_replace = os.replace
    appended = False

    def replace_and_append(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal appended
        original_replace(source, destination)
        if not appended and Path(source) == path:
            appended = True
            # Simulate an operator opening the inbox in the tiny interval after
            # rotation but before the downloader recreates its header.
            with path.open(mode="a", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(["456", ""])

    monkeypatch.setattr(os, "replace", replace_and_append)
    queue = queue_factory(path)

    assert set(fake_store.download_requests) == {123}
    monkeypatch.setattr(os, "replace", original_replace)

    requests = queue.download_requests()

    assert {request.gid for request in requests} == {123, 456}
    with path.open(newline="", encoding="utf-8") as file:
        assert list(csv.reader(file)) == [["gid", "url"]]


def test_old_file_descriptor_append_keeps_claim_for_next_replay(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
    tmp_path: Path,
) -> None:
    path = tmp_path / "todownload_gids.csv"
    with path.open(mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["gid", "url"])
        writer.writerow(["123", ""])

    old_descriptor = path.open(mode="a", newline="", encoding="utf-8")
    old_writer = csv.writer(old_descriptor)

    def append_through_old_descriptor() -> None:
        fake_store.request_download_observer = None
        old_writer.writerow(["456", ""])
        old_descriptor.flush()

    fake_store.request_download_observer = append_through_old_descriptor
    try:
        queue = queue_factory(path)
    finally:
        old_descriptor.close()

    assert set(fake_store.download_requests) == {123}
    assert len(claim_paths(path)) == 1

    requests = queue.download_requests()

    assert {request.gid for request in requests} == {123, 456}
    assert claim_paths(path) == []


def test_crash_claims_replay_in_creation_order(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
    tmp_path: Path,
) -> None:
    path = tmp_path / "todownload_gids.csv"
    old_claim = path.with_name(f".{path.name}.claim-{1:020d}-old")
    new_claim = path.with_name(f".{path.name}.claim-{2:020d}-new")
    for claim_path, url in (
        (old_claim, "https://exhentai.org/g/123/aaaaaaaaaa/"),
        (new_claim, "https://exhentai.org/g/123/bbbbbbbbbb/"),
    ):
        with claim_path.open(mode="w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(["gid", "url"])
            writer.writerow(["123", url])

    queue_factory(path)

    assert (
        fake_store.download_requests[123].url
        == "https://exhentai.org/g/123/bbbbbbbbbb/"
    )
    assert claim_paths(path) == []


def test_durable_request_survives_construction_and_is_read_live(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    request = DownloadRequest(
        1, "https://exhentai.org/g/1/abcdef0123/", "existing-token"
    )
    fake_store.download_requests = {1: request}

    queue = queue_factory()

    assert queue.download_requests() == [request]


def test_request_download_returns_token_and_completion_is_conditional(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()
    first = queue.request_download(1, "https://exhentai.org/g/1/abcdef0123/")
    second = queue.request_download(1)

    assert first.token != second.token
    assert second.url == first.url

    queue.complete_download_request(first)
    assert fake_store.download_requests[1] == second

    queue.complete_download_request(second)
    assert fake_store.download_requests == {}


def test_request_methods_reject_mismatched_gid_and_url_before_core(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()

    with pytest.raises(ValueError, match="42 does not match URL GID 1"):
        queue.request_download(42, "https://exhentai.org/g/1/abcdef0123/")
    with pytest.raises(ValueError, match="42 does not match URL GID 1"):
        queue.ensure_download_request(42, "https://exhentai.org/g/1/abcdef0123/")

    assert fake_store.download_requests == {}


def test_ensure_download_request_preserves_an_existing_token(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()
    existing = queue.request_download(
        1,
        "https://exhentai.org/g/1/abcdef0123/",
    )

    preserved = queue.ensure_download_request(
        1,
        "https://exhentai.org/g/1/abcdef0124/",
    )
    created = queue.ensure_download_request(
        2,
        "https://exhentai.org/g/2/abcdef0123/",
    )

    assert not preserved.created
    assert preserved.request == existing
    assert fake_store.download_requests[1] == existing
    assert created.created
    assert created.request == fake_store.download_requests[2]


def test_queue_delegates_request_lifecycle_to_coordinator(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()

    request = queue.request_download(1)
    queue.complete_download_request(request)

    assert fake_store.download_requests == {}


def test_download_turn_operations_delegate_to_coordinator(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()

    turn = queue.claim_download_turn(lease_seconds=300)

    assert turn is not None
    assert queue.renew_download_turn(turn, lease_seconds=300)
    assert queue.completed_gallery_ingest_generation() == 0
    assert queue.request_gallery_ingest(turn)
    assert queue.completed_gallery_ingest_generation() == turn.generation
    assert fake_store.claim_download_turn_calls == [300]
    assert fake_store.renew_download_turn_calls == [(turn, 300)]
    assert fake_store.gallery_ingest_state_reads == 2


def test_download_turn_handoff_is_idempotent(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
) -> None:
    queue = queue_factory()
    turn = queue.claim_download_turn(lease_seconds=300)
    assert turn is not None

    assert queue.request_gallery_ingest(turn)
    assert queue.request_gallery_ingest(turn)


def test_finish_download_turn_atomically_hands_off_and_completes_exact_request(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
) -> None:
    queue = queue_factory()
    request = queue.request_download(1)
    turn = queue.claim_download_turn(lease_seconds=300)
    assert turn is not None

    assert queue.finish_download_turn(turn, request)

    assert fake_store.download_requests == {}
    assert fake_store.completed_ingest_generation == turn.generation


def test_requests_can_settle_inside_a_live_turn_before_one_batch_handoff(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
) -> None:
    queue = queue_factory()
    completed_request = queue.request_download(1)
    missing_request = queue.request_download(404)
    turn = queue.claim_download_turn(lease_seconds=300)
    assert turn is not None

    assert queue.complete_download_request_in_turn(turn, completed_request)
    assert queue.complete_missing_download_request_in_turn(
        turn,
        missing_request,
        404,
    )

    assert fake_store.download_requests == {}
    assert fake_store.removed_gids == {404}
    assert fake_store.handed_off_turn is None
    assert fake_store.active_download_turn == turn

    assert turn is not None
    assert queue.request_gallery_ingest(turn)
    assert fake_store.completed_ingest_generation == turn.generation


def test_stale_download_turn_cannot_renew_or_handoff(
    queue_factory: Callable[..., GalleryQueue],
    fake_store: FakeDBStore,
) -> None:
    queue = queue_factory()
    stale_turn = DownloadTurn(99, "stale", 10_000)
    request = queue.request_download(1)

    assert not queue.renew_download_turn(stale_turn, lease_seconds=300)
    assert not queue.request_gallery_ingest(stale_turn)
    assert not queue.finish_download_turn(stale_turn, request)
    assert fake_store.download_requests == {1: request}


def test_request_identity_uses_gid_and_token_not_mutable_url(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()
    request = queue.request_download(1, "https://exhentai.org/g/1/abcdef0123/")
    fake_store.download_requests[1] = DownloadRequest(
        gid=1,
        url="https://e-hentai.org/g/1/abcdef0123/",
        token=request.token,
    )

    assert queue.is_current(request)
    queue.complete_download_request(request)
    assert fake_store.download_requests == {}


def test_should_attempt_reads_database_state_live(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()
    assert queue.should_attempt(1)

    fake_store.gids.add(1)
    assert not queue.should_attempt(1)

    fake_store.pending_download_gids.append(1)
    assert queue.should_attempt(1)

    fake_store.pending_download_gids.clear()
    queue.request_download(1)
    assert queue.should_attempt(1)


def test_pending_redownload_gids_reads_database_state_live(
    queue_factory: Callable[..., GalleryQueue], fake_store: FakeDBStore
) -> None:
    queue = queue_factory()
    assert queue.pending_redownload_gids() == []

    fake_store.pending_download_gids.append(1)
    assert queue.pending_redownload_gids() == [1]


def test_csv_path_none_disables_manual_queue_without_touching_filesystem(
    fake_coordinator: FakeCoordinator, tmp_path: Path
) -> None:
    queue = GalleryQueue(coordinator=fake_coordinator, csv_path=None)

    assert queue.download_requests() == []
    assert list(tmp_path.iterdir()) == []

    request = queue.request_download(1, "https://exhentai.org/g/1/abcdef0123/")
    assert queue.download_requests() == [request]


def test_skip_and_success_update_wocount(
    queue_factory: Callable[..., GalleryQueue],
) -> None:
    queue = queue_factory()
    queue.wocount = 5

    queue.note_skip()
    assert queue.wocount == 6

    queue.note_download_success()
    assert queue.wocount == 0
