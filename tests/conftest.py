"""Shared fixtures: the Windows event loop policy and a real PostgreSQL for server-mode tests.

PostgreSQL runs in a ``pgvector/pgvector:pg16`` container started once per
session through testcontainers; each test gets a fresh database in it. Tests
that need it are marked ``postgres`` (the personal-mode CI job deselects them).
When Docker is unavailable those tests are skipped locally, but fail when
``SPARK_TEST_POSTGRES=required`` (the server-mode CI job sets it).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

PG_IMAGE = "pgvector/pgvector:pg16"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "postgres: needs a real PostgreSQL (testcontainers)")


def _new_loop() -> asyncio.AbstractEventLoop:
    # psycopg's async driver cannot run on the Proactor loop (the Windows default).
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    return {"loop": _new_loop}


def _postgres_required() -> bool:
    return os.environ.get("SPARK_TEST_POSTGRES", "").lower() == "required"


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str]:
    """A running PostgreSQL server; yields an admin URL (database ``postgres``)."""
    external = os.environ.get("SPARK_TEST_POSTGRES_URL")
    if external:
        yield external
        return
    try:
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(
            PG_IMAGE,
            username="spark",
            password="spark-test-dev-only",
            dbname="postgres",
            driver=None,
        )
        container.start()
    except Exception as exc:  # Docker missing or not running
        if _postgres_required():
            pytest.fail(f"PostgreSQL is required but could not start: {exc}")
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def _with_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture
def pg_url(pg_server: str) -> Iterator[str]:
    """A fresh, empty database on the session's PostgreSQL server."""
    import psycopg

    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_database(pg_server, name)
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def _sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One SQLite database migrated to head per session; tests get a copy."""
    from spark.db import migrate
    from spark.mode import DatabaseSettings, Dialect

    path = tmp_path_factory.mktemp("template") / "spark.db"
    migrate.upgrade(DatabaseSettings(Dialect.SQLITE, sqlite_path=path))
    return path


@pytest.fixture(scope="session")
def _pg_template(pg_server: str) -> str:
    """One PostgreSQL database migrated to head per session; tests get a clone."""
    import psycopg

    from spark.db import migrate
    from spark.mode import DatabaseSettings, Dialect

    name = f"tpl_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    migrate.upgrade(DatabaseSettings(Dialect.POSTGRESQL, url=_with_database(pg_server, name)))
    return name


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def spark_db_settings(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    """Settings for a fresh SPARK database at head: SQLite, and (marked postgres) PostgreSQL."""
    import shutil

    import psycopg

    from spark.mode import DatabaseSettings, Dialect

    if request.param == "sqlite":
        path = tmp_path / "spark.db"
        shutil.copy(request.getfixturevalue("_sqlite_template"), path)
        yield DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
        return
    server = request.getfixturevalue("pg_server")
    template = request.getfixturevalue("_pg_template")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=_with_database(server, name))
    finally:
        with psycopg.connect(server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def spark_db(spark_db_settings: Any) -> AsyncIterator[Any]:
    """An open SPARK database at head, on the event loop of the test."""
    from spark.db import Database

    db = await Database.open(spark_db_settings)
    try:
        yield db
    finally:
        await db.close()
