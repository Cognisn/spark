"""Shared fixtures: the Windows event loop policy and a real PostgreSQL for server-mode tests.

PostgreSQL runs in a ``pgvector/pgvector:pg16`` container started once per
session through testcontainers; each test gets a fresh database in it. Tests
that need it are marked ``postgres`` (the personal-mode CI job deselects them).
When Docker is unavailable those tests are skipped locally, but fail when
``SPARK_TEST_POSTGRES=required`` (the server-mode CI job sets it).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

PG_IMAGE = "pgvector/pgvector:pg16"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "postgres: needs a real PostgreSQL (testcontainers)")
    config.addinivalue_line(
        "markers",
        "integration: needs a live external service (run by hand; CI deselects it)",
    )


def _can_load_embeddings(items: list[pytest.Item]) -> bool:
    """True when the session runs a test outside ``tests/scripts/`` (which never loads the model)."""
    scripts = Path(__file__).parent / "scripts"
    return any(scripts not in Path(str(item.path)).parents for item in items)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Load the embedding model once, before the first test, outside any test's timeout.

    Importing sentence_transformers and loading the model takes tens of seconds on a busy
    machine; whichever test first indexed a message would be charged that and could exceed
    pytest-timeout's limit. The model and device are the defaults a vector index uses, so
    the class-wide cache key matches. A failure never fails the session: the tests load the
    model themselves, as they would without this hook.
    """
    log = logging.getLogger("spark.tests.warmup")
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    def say(message: str) -> None:
        log.info(message)
        if reporter is not None:
            reporter.write_line(message)

    if not _can_load_embeddings(list(session.items)):
        return
    started = time.perf_counter()
    try:
        from spark.index.embeddings import DEFAULT_MODEL, SentenceTransformerEmbedding

        SentenceTransformerEmbedding(DEFAULT_MODEL, device="cpu").encode("warmup")
    except Exception as exc:  # noqa: BLE001 - warm-up is best effort
        say(f"embedding warm-up skipped after {time.perf_counter() - started:.1f} s: {exc!r}")
        return
    say(f"embedding warm-up: model loaded in {time.perf_counter() - started:.1f} s")


def _new_loop() -> asyncio.AbstractEventLoop:
    # psycopg's async driver cannot run on the Proactor loop (the Windows default).
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    return {"loop": _new_loop}


@pytest.fixture(autouse=True)
def _reset_access_check() -> Iterator[None]:
    """A server-mode app installs warden's access check process-wide: restore the stub."""
    yield
    from spark.instance_settings import access

    access.reset()


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
    """One PostgreSQL database at head (warden's chain too) per session; tests get a clone."""
    import psycopg

    from spark.db import migrate
    from spark.identity import migrations as identity_migrations
    from spark.mode import DatabaseSettings, Dialect

    name = f"tpl_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=_with_database(pg_server, name))
    migrate.upgrade(settings)
    identity_migrations.upgrade(settings)
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


@pytest.fixture
def sqlite_settings(_sqlite_template: Path, tmp_path: Path) -> Any:
    """Settings for a fresh SQLite database at head (personal mode)."""
    import shutil

    from spark.mode import DatabaseSettings, Dialect

    path = tmp_path / "spark.db"
    shutil.copy(_sqlite_template, path)
    return DatabaseSettings(Dialect.SQLITE, sqlite_path=path)


@pytest.fixture
def sync_db(sqlite_settings: Any) -> Iterator[Any]:
    """The bridge (SyncDatabase) to a fresh SQLite database whose loop runs in a thread.

    For tests of synchronous code (ConversationManager, orchestrators, builders,
    indexes). Seed or inspect with ``sync_db.call(repo.fn, ...)`` or the raw
    helpers ``sync_db.execute`` / ``fetch_value`` / ``fetch_one`` / ``fetch_all``
    (``?`` placeholders; ``... RETURNING id`` for new ids).
    """
    from spark.db.standalone import open_standalone

    handle = open_standalone(sqlite_settings, auto_migrate=True)
    try:
        yield handle.sync_db
    finally:
        handle.close()
