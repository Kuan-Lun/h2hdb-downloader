from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from h2hdb import (
    H2HDB,
    CoordinatorUnavailableError,
    CoreConfig,
    DatabaseConfig,
    DownloadCoordinator,
    DownloadTurn,
)

from h2hdb_downloader.downloader import Downloader, GalleryDriver


@pytest.fixture
def sqlite_coordinator(tmp_path: Path) -> H2HDB:
    coordinator = H2HDB(
        CoreConfig(
            database=DatabaseConfig(
                sql_type="sqlite",
                database=str(tmp_path / "coordination.sqlite3"),
            )
        )
    )
    coordinator.migrate()
    coordinator.check_compatibility()
    initial_ingest = coordinator.claim_gallery_ingest(
        lease_seconds=60,
        periodic_scan=False,
    )
    assert initial_ingest is not None
    assert coordinator.complete_gallery_ingest(initial_ingest)
    return coordinator


def make_downloader(coordinator: DownloadCoordinator) -> Downloader:
    return Downloader(
        cast(GalleryDriver, object()),
        coordinator=coordinator,
        csv_path=None,
        wait4client=0,
        retry2download=0,
        turn_poll_seconds=0.001,
    )


def test_queue_wrappers_use_public_core_sqlite_port(
    sqlite_coordinator: H2HDB,
) -> None:
    downloader = make_downloader(sqlite_coordinator)
    existing = downloader._queue.request_download(1)

    ensured_existing = downloader._queue.ensure_download_request(
        1,
        "https://exhentai.org/g/1/abcdef0123/",
    )
    ensured_missing = downloader._queue.ensure_download_request(404)
    turn = downloader._queue.claim_download_turn(lease_seconds=60)
    assert turn is not None

    assert not ensured_existing.created
    assert ensured_existing.request.token == existing.token
    assert ensured_existing.request.url == "https://exhentai.org/g/1/abcdef0123/"
    assert ensured_missing.created
    assert downloader._queue.complete_download_request_in_turn(
        turn,
        ensured_existing.request,
    )
    assert downloader._queue.complete_missing_download_request_in_turn(
        turn,
        ensured_missing.request,
        404,
    )
    assert sqlite_coordinator.get_download_request(1) is None
    assert sqlite_coordinator.get_download_request(404) is None
    assert downloader._queue.request_gallery_ingest(turn)


async def test_claim_turn_retries_backend_neutral_unavailability(
    downloader_factory: Callable[..., Downloader],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    expected = DownloadTurn(7, "turn-token", 10_000)
    attempts = 0
    poll_intervals: list[float] = []

    def claim(*, lease_seconds: int) -> DownloadTurn:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CoordinatorUnavailableError("temporarily unavailable")
        return expected

    async def record_sleep(seconds: float) -> None:
        poll_intervals.append(seconds)

    monkeypatch.setattr(downloader._queue, "claim_download_turn", claim)
    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        record_sleep,
    )

    assert await downloader._claim_download_turn() == expected
    assert poll_intervals == [downloader.turn_poll_seconds]


async def test_completed_generation_retries_backend_neutral_unavailability(
    downloader_factory: Callable[..., Downloader],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    turn = DownloadTurn(7, "turn-token", 10_000)
    attempts = 0
    poll_intervals: list[float] = []

    def completed_generation() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CoordinatorUnavailableError("temporarily unavailable")
        return turn.generation

    async def record_sleep(seconds: float) -> None:
        poll_intervals.append(seconds)

    monkeypatch.setattr(
        downloader._queue,
        "completed_gallery_ingest_generation",
        completed_generation,
    )
    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        record_sleep,
    )

    await downloader._wait_for_gallery_ingest(turn)
    assert poll_intervals == [downloader.turn_poll_seconds]


@pytest.mark.parametrize("polling_boundary", ["claim", "completed_generation"])
async def test_unexpected_coordinator_error_is_not_retried(
    polling_boundary: str,
    downloader_factory: Callable[..., Downloader],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    error = RuntimeError("unexpected coordinator failure")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    sleep_calls: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        record_sleep,
    )
    if polling_boundary == "claim":
        monkeypatch.setattr(downloader._queue, "claim_download_turn", fail)
        with pytest.raises(RuntimeError) as raised:
            await downloader._claim_download_turn()
    else:
        turn = DownloadTurn(7, "turn-token", 10_000)
        monkeypatch.setattr(
            downloader._queue,
            "completed_gallery_ingest_generation",
            fail,
        )
        with pytest.raises(RuntimeError) as raised:
            await downloader._wait_for_gallery_ingest(turn)

    assert raised.value is error
    assert sleep_calls == []
