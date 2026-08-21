from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from h2hdb import (
    CoreConfig,
    DatabaseConfig,
    DownloadHandoff,
    DownloadIngestUnavailableError,
    DownloadTurn,
    HandoffKind,
    VNextDatabaseAdminFacade,
    VNextDownloadQueueFacade,
)

from h2hdb_downloader.downloader import Downloader, GalleryDriver
from tests.conftest import fake_token, fake_turn


@pytest.fixture
def sqlite_facade(tmp_path: Path) -> VNextDownloadQueueFacade:
    config = CoreConfig(
        database=DatabaseConfig(
            sql_type="sqlite",
            database=str(tmp_path / "coordination.sqlite3"),
        )
    )
    VNextDatabaseAdminFacade(config).initialize()
    return VNextDownloadQueueFacade(config)


def make_downloader(facade: VNextDownloadQueueFacade) -> Downloader:
    return Downloader(
        cast(GalleryDriver, object()),
        facade=facade,
        csv_path=None,
        wait4client=0,
        retry2download=0,
        turn_poll_seconds=0.001,
    )


def test_queue_wrappers_use_public_core_sqlite_facade(
    sqlite_facade: VNextDownloadQueueFacade,
) -> None:
    downloader = make_downloader(sqlite_facade)
    existing = downloader._queue.request_download(1)

    ensured_existing = downloader._queue.ensure_download_request(
        1,
        "https://exhentai.org/g/1/abcdef0123/",
    )
    ensured_missing = downloader._queue.ensure_download_request(404)
    turn = downloader._queue.claim_download_turn(lease_seconds=60)

    assert not ensured_existing.created
    assert ensured_existing.request.request_token == existing.request_token
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
    assert sqlite_facade.get_download_request(1) is None
    assert sqlite_facade.get_download_request(404) is None
    assert downloader._queue.handoff_download_turn(turn).download_generation == 1


async def test_claim_turn_retries_backend_neutral_unavailability(
    downloader_factory: Callable[..., Downloader],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    expected = fake_turn(7)
    attempts = 0
    poll_intervals: list[float] = []

    def claim(*, lease_seconds: int) -> DownloadTurn:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DownloadIngestUnavailableError("temporarily unavailable")
        return expected

    async def record_sleep(seconds: float) -> None:
        poll_intervals.append(seconds)

    monkeypatch.setattr(downloader._queue, "claim_download_turn", claim)
    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        record_sleep,
    )

    assert (await downloader._claim_download_turn()).turn == expected
    assert poll_intervals == [downloader.turn_poll_seconds]


async def test_handoff_completion_retries_backend_neutral_unavailability(
    downloader_factory: Callable[..., Downloader],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    handoff = DownloadHandoff(7, fake_token("turn-7"), HandoffKind.DOWNLOADER, 8)
    attempts = 0
    poll_intervals: list[float] = []

    def is_complete(_handoff: DownloadHandoff) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DownloadIngestUnavailableError("temporarily unavailable")
        return True

    async def record_sleep(seconds: float) -> None:
        poll_intervals.append(seconds)

    monkeypatch.setattr(
        downloader._queue,
        "is_download_handoff_complete",
        is_complete,
    )
    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        record_sleep,
    )

    await downloader._wait_for_gallery_ingest(handoff)
    assert poll_intervals == [downloader.turn_poll_seconds]


@pytest.mark.parametrize("polling_boundary", ["claim", "handoff"])
async def test_unexpected_facade_error_is_not_retried(
    polling_boundary: str,
    downloader_factory: Callable[..., Downloader],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = downloader_factory()
    error = RuntimeError("unexpected facade failure")

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
        handoff = DownloadHandoff(
            7,
            fake_token("turn-7"),
            HandoffKind.DOWNLOADER,
            8,
        )
        monkeypatch.setattr(
            downloader._queue,
            "is_download_handoff_complete",
            fail,
        )
        with pytest.raises(RuntimeError) as raised:
            await downloader._wait_for_gallery_ingest(handoff)

    assert raised.value is error
    assert sleep_calls == []
