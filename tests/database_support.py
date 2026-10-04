from __future__ import annotations

import os
import pickle
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import mysql.connector
import pytest
from h2hdb import CoreConfig, DatabaseConfig


@dataclass(frozen=True)
class DatabaseCase:
    config: CoreConfig

    @property
    def backend(self) -> str:
        return self.config.database.sql_type

    @contextmanager
    def connection(self) -> Iterator[Any]:
        database = self.config.database
        if self.backend == "sqlite":
            with closing(sqlite3.connect(database.database)) as connection:
                yield connection
            return
        with closing(
            mysql.connector.connect(
                host=database.host,
                port=database.port,
                user=database.user,
                password=database.password,
                database=database.database,
            )
        ) as connection:
            yield connection

    def execute(self, statement: str, values: Sequence[object] = ()) -> None:
        with self.connection() as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    statement.replace("%s", "?")
                    if self.backend == "sqlite"
                    else statement,
                    tuple(values),
                )
            connection.commit()

    def snapshot(self) -> bytes:
        if self.backend == "sqlite":
            return sha256(Path(self.config.database.database).read_bytes()).digest()
        with self.connection() as connection, closing(connection.cursor()) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s "
                "ORDER BY TABLE_NAME",
                (self.config.database.database,),
            )
            tables = tuple((str(row[0]), str(row[1])) for row in cursor.fetchall())
            digest = sha256()
            for table, kind in tables:
                quoted = "`" + table.replace("`", "``") + "`"
                cursor.execute(f"SHOW CREATE TABLE {quoted}")
                digest.update(pickle.dumps(tuple(cursor.fetchone())))
                if kind != "BASE TABLE":
                    continue
                cursor.execute(f"SELECT * FROM {quoted}")
                rows = sorted(pickle.dumps(tuple(row)) for row in cursor.fetchall())
                digest.update(table.encode() + b"\0")
                for row in rows:
                    digest.update(len(row).to_bytes(8, "big") + row)
            connection.rollback()
            return digest.digest()


@pytest.fixture(scope="session")
def mariadb_server() -> Iterator[dict[str, object]]:
    if os.environ.get("H2HDB_TEST_MARIADB") != "1":
        pytest.skip("disposable MariaDB integration requires H2HDB_TEST_MARIADB=1")
    from testcontainers.community.mysql import MySqlContainer

    with MySqlContainer(
        "mariadb:10.11.11",
        username="consumer_test",
        password="synthetic-consumer-password",
        root_password="synthetic-root-password",
        dbname="consumer_test",
    ).with_kwargs(
        labels={"h2hdb.test-owner": os.environ.get("H2HDB_TEST_OWNER", uuid4().hex)}
    ) as container:
        parameters: dict[str, object] = {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(3306)),
            "user": "root",
            "password": "synthetic-root-password",
        }
        with closing(mysql.connector.connect(**parameters)) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute("SELECT VERSION()")
                row = cursor.fetchone()
                assert isinstance(row, tuple) and str(row[0]).startswith("10.11.11-")
        yield parameters


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("mariadb", marks=(pytest.mark.mariadb, pytest.mark.deep)),
    ]
)
def database_case(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[DatabaseCase]:
    if request.param == "sqlite":
        yield DatabaseCase(
            CoreConfig(
                database=DatabaseConfig(
                    sql_type="sqlite", database=str(tmp_path / "catalog.sqlite3")
                )
            )
        )
        return
    parameters = request.getfixturevalue("mariadb_server")
    database = "consumer_" + uuid4().hex
    with closing(mysql.connector.connect(**parameters)) as connection:
        with closing(connection.cursor()) as cursor:
            cursor.execute(f"CREATE DATABASE `{database}`")
        try:
            yield DatabaseCase(
                CoreConfig(
                    database=DatabaseConfig.model_validate(
                        {**parameters, "sql_type": "mariadb", "database": database}
                    )
                )
            )
        finally:
            with closing(connection.cursor()) as cursor:
                cursor.execute(f"DROP DATABASE `{database}`")
