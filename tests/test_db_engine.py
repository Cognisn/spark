"""Tests for spark.db.engine on SQLite and on a real PostgreSQL."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from spark.db import engine
from spark.db.engine import Connection, Database, DatabaseError, Executor
from spark.mode import DatabaseSettings, Dialect

TABLE = (
    "CREATE TABLE items (id {pk}, name TEXT NOT NULL, flag INTEGER DEFAULT 0, "
    "score REAL, data {blob}, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
)


def _ddl(dialect: Dialect) -> str:
    if dialect is Dialect.SQLITE:
        return TABLE.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", blob="BLOB")
    return TABLE.format(pk="SERIAL PRIMARY KEY", blob="BYTEA")


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
async def db(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Database]:
    if request.param == "sqlite":
        settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "t.db")
    else:
        url = request.getfixturevalue("pg_url")
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=url)
    database = await Database.open(settings, max_size=8)
    await database.execute(_ddl(database.dialect))
    try:
        yield database
    finally:
        await database.close()


class TestStatements:
    async def test_insert_returning_and_fetch(self, db: Database):
        new_id = await db.fetch_value(
            "INSERT INTO items (name, score) VALUES (?, ?) RETURNING id", ("alpha", 0.4)
        )
        row = await db.fetch_one("SELECT * FROM items WHERE id = ?", (new_id,))
        assert row is not None
        assert row["name"] == "alpha"
        assert row["score"] == pytest.approx(0.4)
        assert isinstance(row, dict)

    async def test_fetch_one_missing_is_none(self, db: Database):
        assert await db.fetch_one("SELECT * FROM items WHERE id = ?", (999,)) is None

    async def test_fetch_value_missing_is_none(self, db: Database):
        assert await db.fetch_value("SELECT name FROM items WHERE id = ?", (999,)) is None

    async def test_fetch_all_preserves_order(self, db: Database):
        await db.execute_many("INSERT INTO items (name) VALUES (?)", [("a",), ("b",), ("c",)])
        rows = await db.fetch_all("SELECT name FROM items ORDER BY name DESC")
        assert [r["name"] for r in rows] == ["c", "b", "a"]

    async def test_execute_returns_rowcount(self, db: Database):
        await db.execute_many("INSERT INTO items (name) VALUES (?)", [("a",), ("b",)])
        assert await db.execute("UPDATE items SET flag = ?", (1,)) == 2

    async def test_bool_parameters_are_stored_as_integers(self, db: Database):
        await db.execute("INSERT INTO items (name, flag) VALUES (?, ?)", ("x", True))
        assert await db.fetch_value("SELECT flag FROM items") == 1

    async def test_like_with_percent_and_placeholder(self, db: Database):
        await db.execute_many("INSERT INTO items (name) VALUES (?)", [("apple",), ("banana",)])
        rows = await db.fetch_all("SELECT name FROM items WHERE name LIKE '%an%' AND id > ?", (0,))
        assert [r["name"] for r in rows] == ["banana"]

    async def test_timestamps_are_returned_as_text(self, db: Database):
        await db.execute("INSERT INTO items (name) VALUES (?)", ("t",))
        value = await db.fetch_value("SELECT created_at FROM items")
        assert isinstance(value, str)
        assert value[:4].isdigit() and value[4] == "-"

    async def test_blobs_round_trip_as_bytes(self, db: Database):
        await db.execute("INSERT INTO items (name, data) VALUES (?, ?)", ("b", b"\x00\x01\xff"))
        assert await db.fetch_value("SELECT data FROM items") == b"\x00\x01\xff"


class TestTransactions:
    async def test_commit(self, db: Database):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO items (name) VALUES (?)", ("kept",))
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 1

    async def test_rollback_on_error(self, db: Database):
        with pytest.raises(RuntimeError, match="boom"):
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO items (name) VALUES (?)", ("lost",))
                raise RuntimeError("boom")
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 0

    async def test_callers_connection_joins_the_outer_transaction(self, db: Database):
        """A writer given the caller's connection (as warden's audit will be) shares its fate."""

        async def audit(conn: Connection) -> None:
            async with db.transaction(conn) as inner:
                assert inner is conn
                await inner.execute("INSERT INTO items (name) VALUES (?)", ("audit",))

        with pytest.raises(RuntimeError):
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO items (name) VALUES (?)", ("write",))
                await audit(conn)
                raise RuntimeError("outer fails")
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 0

        async with db.transaction() as conn:
            await conn.execute("INSERT INTO items (name) VALUES (?)", ("write",))
            await audit(conn)
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 2

    async def test_inner_failure_rolls_back_only_the_savepoint(self, db: Database):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO items (name) VALUES (?)", ("outer",))
            with pytest.raises(RuntimeError):
                async with db.transaction(conn) as inner:
                    await inner.execute("INSERT INTO items (name) VALUES (?)", ("inner",))
                    raise RuntimeError("inner fails")
        rows = await db.fetch_all("SELECT name FROM items")
        assert [r["name"] for r in rows] == ["outer"]

    async def test_uncommitted_writes_are_invisible_to_other_connections(self, db: Database):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO items (name) VALUES (?)", ("pending",))
            assert await db.fetch_value("SELECT COUNT(*) FROM items") == 0
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 1


class TestConcurrency:
    async def test_parallel_tasks_each_get_their_own_results(self, db: Database):
        """The 0.2.0 shared connection lost results across threads; the pool must not."""

        async def one(i: int) -> tuple[int, int]:
            async with db.transaction() as conn:
                new_id = await conn.fetch_value(
                    "INSERT INTO items (name, flag) VALUES (?, ?) RETURNING id", (f"n{i}", i)
                )
                flag = await conn.fetch_value("SELECT flag FROM items WHERE id = ?", (new_id,))
            return i, flag

        results = await asyncio.gather(*(one(i) for i in range(40)))
        assert all(i == flag for i, flag in results)
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 40

    async def test_concurrent_readers_and_a_writer(self, db: Database):
        stop = asyncio.Event()

        async def writer() -> None:
            for i in range(50):
                async with db.transaction() as conn:
                    await conn.execute("INSERT INTO items (name) VALUES (?)", (f"w{i}",))
            stop.set()

        async def reader() -> int:
            reads = 0
            while not stop.is_set():
                count = await db.fetch_value("SELECT COUNT(*) FROM items")
                assert count is not None
                reads += 1
                await asyncio.sleep(0)
            return reads

        _, *reads = await asyncio.gather(writer(), *(reader() for _ in range(5)))
        assert all(r > 0 for r in reads)
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 50


class TestOpen:
    async def test_records_its_loop_and_dialect(self, db: Database):
        assert db.loop is asyncio.get_running_loop()
        assert db.dialect in (Dialect.SQLITE, Dialect.POSTGRESQL)

    async def test_sqlite_creates_parent_directories(self, tmp_path: Path):
        path = tmp_path / "nested" / "dir" / "spark.db"
        database = await Database.open(DatabaseSettings(Dialect.SQLITE, sqlite_path=path))
        await database.close()
        assert path.exists()

    async def test_sqlite_requires_returning_support(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "_sqlite_version_info", lambda: (3, 34, 1))
        with pytest.raises(DatabaseError, match="3.35"):
            await Database.open(DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "x.db"))

    async def test_sqlite_enforces_foreign_keys(self, tmp_path: Path):
        database = await Database.open(
            DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "fk.db")
        )
        try:
            await database.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
            await database.execute("CREATE TABLE c (p_id INTEGER REFERENCES p(id))")
            with pytest.raises(Exception):
                await database.execute("INSERT INTO c (p_id) VALUES (?)", (42,))
        finally:
            await database.close()

    async def test_closed_database_refuses_work(self, tmp_path: Path):
        database = await Database.open(
            DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "c.db")
        )
        await database.close()
        with pytest.raises(DatabaseError, match="closed"):
            await database.fetch_value("SELECT 1")


@pytest.mark.postgres
class TestPostgresSpecific:
    async def test_pool_and_raw_connection_are_exposed_for_warden(self, pg_url: str):
        import psycopg
        from psycopg_pool import AsyncConnectionPool

        database = await Database.open(DatabaseSettings(Dialect.POSTGRESQL, url=pg_url))
        try:
            assert isinstance(database.pool, AsyncConnectionPool)
            async with database.transaction() as conn:
                assert isinstance(conn.raw, psycopg.AsyncConnection)
        finally:
            await database.close()

    async def test_search_path_puts_the_spark_schema_first(self, pg_url: str):
        database = await Database.open(DatabaseSettings(Dialect.POSTGRESQL, url=pg_url))
        try:
            path = await database.fetch_value("SHOW search_path")
            assert path.replace(" ", "").startswith("spark,")
        finally:
            await database.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor loop exists only on Windows")
def test_postgres_refuses_the_proactor_loop_on_windows():
    loop = asyncio.ProactorEventLoop()  # type: ignore[attr-defined]
    try:
        with pytest.raises(DatabaseError, match="Selector"):
            engine.check_event_loop(loop, Dialect.POSTGRESQL)
        engine.check_event_loop(loop, Dialect.SQLITE)  # aiosqlite is fine on any loop
    finally:
        loop.close()


class TestExecutorTransactions:
    async def test_connection_transaction_nests_in_the_callers(self, db: Database):
        """Repositories call executor.transaction(); on a Connection it joins the caller's."""

        async def unit_of_work(ex: Executor) -> None:
            async with ex.transaction() as conn:
                await conn.execute("INSERT INTO items (name) VALUES (?)", ("unit",))

        with pytest.raises(RuntimeError):
            async with db.transaction() as outer:
                await unit_of_work(outer)
                raise RuntimeError("caller fails")
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 0

        await unit_of_work(db)
        assert await db.fetch_value("SELECT COUNT(*) FROM items") == 1


class TestTimestamps:
    async def test_utc_iso_timestamps_round_trip(self, db: Database):
        await db.execute(
            "INSERT INTO items (name, created_at) VALUES (?, ?)",
            ("t", "2026-11-01T00:00:00+00:00"),
        )
        assert await db.fetch_value("SELECT created_at FROM items") == "2026-11-01T00:00:00+00:00"

    async def test_database_defaults_are_utc(self, db: Database):
        from datetime import datetime, timezone

        await db.execute("INSERT INTO items (name) VALUES (?)", ("now",))
        value = await db.fetch_value("SELECT created_at FROM items")
        stored = datetime.fromisoformat(value)
        if stored.tzinfo is None:  # SQLite's CURRENT_TIMESTAMP text is UTC without an offset
            stored = stored.replace(tzinfo=timezone.utc)
        assert abs((datetime.now(timezone.utc) - stored).total_seconds()) < 120
