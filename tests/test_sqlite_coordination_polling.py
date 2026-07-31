import sqlite3
from pathlib import Path
from typing import cast

import pytest
from h2hdb import H2HDB, DatabaseConfig, H2HDBConfig
from h2hdb.sqlite_connector import SQLiteConnector
from hbrowser import ExHDriver

from h2hdb_downloader.downloader import Downloader, _is_retryable_sqlite_lock_error


@pytest.fixture
def sqlite_coordination_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> H2HDBConfig:
    # h2hdb's file logger writes relative to cwd. Keep that test artifact in
    # pytest's temporary directory instead of the repository.
    monkeypatch.chdir(tmp_path)
    config = H2HDBConfig(
        database=DatabaseConfig(
            sql_type="sqlite",
            database=str(tmp_path / "coordination.sqlite3"),
        )
    )
    with H2HDB(config=config) as database:
        database.create_main_tables()
        baseline = database._claim_gallery_ingest(
            lease_seconds=60,
            periodic_scan=False,
        )
        assert baseline is not None
        assert database._complete_gallery_ingest(baseline)
    return config


def make_downloader(
    config: H2HDBConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Downloader:
    monkeypatch.setattr(
        "h2hdb_downloader.downloader.load_config",
        lambda _config_path: config,
    )
    return Downloader(
        cast(ExHDriver, object()),
        config_path="unused.json",
        csv_path=None,
        wait4client=0,
        retry2download=0,
        turn_poll_seconds=0.001,
    )


def lock_database_exclusively(config: H2HDBConfig) -> sqlite3.Connection:
    connection = sqlite3.connect(
        config.database.database,
        isolation_level=None,
    )
    connection.execute("BEGIN EXCLUSIVE")
    return connection


def force_new_sqlite_connections_not_to_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = SQLiteConnector.connect

    def connect_without_wait(connector: SQLiteConnector) -> None:
        original_connect(connector)
        connector.connection.execute("PRAGMA busy_timeout = 0")

    monkeypatch.setattr(SQLiteConnector, "connect", connect_without_wait)


def test_batch_queue_wrappers_use_real_sqlite_transactions(
    sqlite_coordination_config: H2HDBConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = make_downloader(sqlite_coordination_config, monkeypatch)
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

    with H2HDB(config=sqlite_coordination_config) as database:
        assert database.get_download_request(1) is None
        assert database.get_download_request(404) is None
        assert database.removed_galleries._check_removed_gallery_gid(404)

    assert downloader._queue.request_gallery_ingest(turn)


@pytest.mark.parametrize("primary_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_extended_sqlite_lock_codes_use_their_primary_code(
    primary_code: int,
) -> None:
    error = sqlite3.OperationalError("extended lock error")
    error.sqlite_errorcode = primary_code | (2 << 8)

    assert _is_retryable_sqlite_lock_error(error)


async def test_claim_turn_retries_real_sqlite_exclusive_lock_then_continues(
    sqlite_coordination_config: H2HDBConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = make_downloader(sqlite_coordination_config, monkeypatch)
    lock_connection = lock_database_exclusively(sqlite_coordination_config)
    force_new_sqlite_connections_not_to_wait(monkeypatch)
    poll_intervals: list[float] = []

    async def release_lock_on_poll(seconds: float) -> None:
        poll_intervals.append(seconds)
        lock_connection.rollback()

    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        release_lock_on_poll,
    )
    try:
        turn = await downloader._claim_download_turn()
    finally:
        lock_connection.close()

    assert turn.generation == 1
    assert poll_intervals == [downloader.turn_poll_seconds]


async def test_completed_generation_retries_real_sqlite_exclusive_lock(
    sqlite_coordination_config: H2HDBConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = make_downloader(sqlite_coordination_config, monkeypatch)
    request = downloader._queue.request_download(123)
    turn = downloader._queue.claim_download_turn(lease_seconds=60)
    assert turn is not None
    assert downloader._queue.finish_download_turn(turn, request)
    with H2HDB(config=sqlite_coordination_config) as database:
        ingest_turn = database._claim_gallery_ingest(
            lease_seconds=60,
            periodic_scan=False,
        )
        assert ingest_turn is not None
        assert database._complete_gallery_ingest(ingest_turn)

    lock_connection = lock_database_exclusively(sqlite_coordination_config)
    force_new_sqlite_connections_not_to_wait(monkeypatch)
    poll_intervals: list[float] = []

    async def release_lock_on_poll(seconds: float) -> None:
        poll_intervals.append(seconds)
        lock_connection.rollback()

    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        release_lock_on_poll,
    )
    try:
        await downloader._wait_for_gallery_ingest(turn)
    finally:
        lock_connection.close()

    assert poll_intervals == [downloader.turn_poll_seconds]


@pytest.mark.parametrize("polling_boundary", ["claim", "completed_generation"])
async def test_non_lock_sqlite_operational_error_is_not_retried(
    polling_boundary: str,
    sqlite_coordination_config: H2HDBConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = make_downloader(sqlite_coordination_config, monkeypatch)
    error: sqlite3.OperationalError | None = None
    with sqlite3.connect(":memory:") as connection:
        try:
            connection.execute("SELECT FROM")
        except sqlite3.OperationalError as caught:
            error = caught
    assert error is not None
    assert error.sqlite_errorcode == sqlite3.SQLITE_ERROR

    def raise_non_lock_error(*_args: object, **_kwargs: object) -> None:
        raise error

    sleep_calls: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        "h2hdb_downloader.downloader.asyncio.sleep",
        record_sleep,
    )
    if polling_boundary == "claim":
        monkeypatch.setattr(
            downloader._queue,
            "claim_download_turn",
            raise_non_lock_error,
        )
        with pytest.raises(sqlite3.OperationalError) as raised:
            await downloader._claim_download_turn()
    else:
        turn = downloader._queue.claim_download_turn(lease_seconds=60)
        assert turn is not None
        monkeypatch.setattr(
            downloader._queue,
            "completed_gallery_ingest_generation",
            raise_non_lock_error,
        )
        with pytest.raises(sqlite3.OperationalError) as raised:
            await downloader._wait_for_gallery_ingest(turn)

    assert raised.value is error
    assert sleep_calls == []
