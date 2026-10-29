"""Async database access: a psycopg 3 pool for PostgreSQL, aiosqlite for SQLite.

:class:`Database` owns the connections and is bound to the event loop that
opened it. Every statement runs on that loop; synchronous code on worker threads
reaches it only through :mod:`spark.db.bridge`.

Outside a transaction each statement commits on its own. :meth:`Database.transaction`
opens a transaction; given a caller's :class:`Connection` it joins the caller's
transaction through a savepoint instead, so a second writer (warden's audit
recorder, in server mode) commits or rolls back together with the caller.

Rows come back as plain ``dict`` objects in both dialects. PostgreSQL date and
time values are returned as ISO 8601 text in UTC (sessions run in UTC), and ``bool``
parameters are stored as integers (the schema has no boolean columns). In server
mode numpy arrays are sent as pgvector ``vector`` values and ``vector`` columns
come back as float32 numpy arrays (:func:`register_vector`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
import sys
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite
from pgvector import Vector

from spark.db.dialect import to_driver_sql
from spark.mode import DatabaseSettings, Dialect

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

__all__ = [
    "Connection",
    "Database",
    "DatabaseError",
    "Executor",
    "Params",
    "Row",
    "check_event_loop",
    "register_vector",
]

Params = Sequence[Any]
Row = dict[str, Any]

POSTGRES_SCHEMA = "spark"
_MIN_SQLITE = (3, 35, 0)  # RETURNING


class DatabaseError(RuntimeError):
    """The data layer cannot do what was asked (closed, unsupported, misconfigured)."""


class Executor(Protocol):
    """Anything repositories can run statements on: a Database or a Connection."""

    @property
    def dialect(self) -> Dialect: ...

    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None: ...

    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]: ...

    async def fetch_value(self, sql: str, params: Params = ()) -> Any: ...

    async def execute(self, sql: str, params: Params = ()) -> int: ...

    async def execute_many(self, sql: str, params_seq: Iterable[Params]) -> None: ...

    def transaction(self) -> AbstractAsyncContextManager[Connection]:
        """A transaction: new on a Database, a savepoint inside a Connection's."""
        ...


def _sqlite_version_info() -> tuple[int, int, int]:
    major, minor, patch = sqlite3.sqlite_version_info
    return major, minor, patch


def check_event_loop(loop: asyncio.AbstractEventLoop, dialect: Dialect) -> None:
    """psycopg's async driver cannot run on the Windows Proactor loop."""
    if dialect is not Dialect.POSTGRESQL or sys.platform != "win32":
        return
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    if proactor is not None and isinstance(loop, proactor):
        raise DatabaseError(
            "PostgreSQL access needs the Selector event loop on Windows "
            "(asyncio.WindowsSelectorEventLoopPolicy)"
        )


async def register_vector(conn: AsyncConnection[Any]) -> None:
    """Adapt numpy arrays to and from pgvector's ``vector`` type on a pool connection.

    Called for every new connection in server mode. Before ``spark migrate`` has
    created the extension there is no type to register, and the connection is
    left as it is.
    """
    from pgvector.psycopg import register_vector_async
    from psycopg import ProgrammingError

    try:
        await register_vector_async(conn)
    except ProgrammingError:
        pass  # "vector type not found": the extension is not installed yet


def _params(params: Params) -> tuple[Any, ...]:
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


def _value(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        # Sessions run in UTC, so a naive TIMESTAMP is UTC; return it as SPARK writes it.
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, Vector):
        return value.to_numpy()
    return value


def _pg_row(row: Mapping[str, Any] | None) -> Row | None:
    if row is None:
        return None
    return {k: _value(v) for k, v in row.items()}


def _sqlite_row(row: sqlite3.Row | None) -> Row | None:
    return None if row is None else dict(row)


class Connection:
    """One connection, held for a transaction or a single statement."""

    def __init__(self, raw: Any, dialect: Dialect) -> None:
        self._raw = raw
        self._dialect = dialect
        self._depth = 0

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    @property
    def raw(self) -> Any:
        """The driver connection (``psycopg.AsyncConnection`` or ``aiosqlite.Connection``)."""
        return self._raw

    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None:
        if self._dialect is Dialect.SQLITE:
            async with self._raw.execute(sql, _params(params)) as cur:
                return _sqlite_row(await cur.fetchone())
        async with self._pg_cursor() as cur:
            await cur.execute(to_driver_sql(sql, self._dialect), _params(params))
            return _pg_row(await cur.fetchone())

    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]:
        if self._dialect is Dialect.SQLITE:
            async with self._raw.execute(sql, _params(params)) as cur:
                return [dict(r) for r in await cur.fetchall()]
        async with self._pg_cursor() as cur:
            await cur.execute(to_driver_sql(sql, self._dialect), _params(params))
            return [r for r in (_pg_row(x) for x in await cur.fetchall()) if r is not None]

    async def fetch_value(self, sql: str, params: Params = ()) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    async def execute(self, sql: str, params: Params = ()) -> int:
        if self._dialect is Dialect.SQLITE:
            async with self._raw.execute(sql, _params(params)) as cur:
                return int(cur.rowcount)
        async with self._raw.cursor() as cur:
            await cur.execute(to_driver_sql(sql, self._dialect), _params(params))
            return int(cur.rowcount)

    async def execute_many(self, sql: str, params_seq: Iterable[Params]) -> None:
        rows = [_params(p) for p in params_seq]
        if not rows:
            return
        if self._dialect is Dialect.SQLITE:
            await self._raw.executemany(sql, rows)
            return
        async with self._raw.cursor() as cur:
            await cur.executemany(to_driver_sql(sql, self._dialect), rows)

    def transaction(self) -> AbstractAsyncContextManager[Connection]:
        """Nest a unit of work in this connection's transaction (a savepoint)."""
        return self._transaction()

    def _pg_cursor(self) -> Any:
        from psycopg.rows import dict_row

        return self._raw.cursor(row_factory=dict_row)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[Connection]:
        if self._dialect is Dialect.POSTGRESQL:
            async with self._raw.transaction():
                yield self
            return
        # SQLite runs in autocommit mode; BEGIN IMMEDIATE takes the write lock up
        # front so two writers never deadlock upgrading from a read lock.
        self._depth += 1
        savepoint = f"sp_{self._depth}"
        begin, commit, rollback = (
            ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK")
            if self._depth == 1
            else (
                f"SAVEPOINT {savepoint}",
                f"RELEASE {savepoint}",
                f"ROLLBACK TO {savepoint}; RELEASE {savepoint}",
            )
        )
        await self._raw.execute(begin)
        try:
            yield self
        except BaseException:
            for stmt in rollback.split("; "):
                await self._raw.execute(stmt)
            raise
        else:
            await self._raw.execute(commit)
        finally:
            self._depth -= 1


class _SqlitePool:
    """A fixed set of aiosqlite connections to one WAL-mode database file."""

    def __init__(self, path: Path, size: int) -> None:
        self._path = path
        self._size = size
        self._idle: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all: list[aiosqlite.Connection] = []

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(self._size):
            conn = await aiosqlite.connect(str(self._path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            self._all.append(conn)
            self._idle.put_nowait(conn)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self._idle.get()
        try:
            yield conn
        finally:
            self._idle.put_nowait(conn)

    async def close(self) -> None:
        for conn in self._all:
            await conn.close()
        self._all.clear()


class Database:
    """The connection pool for one database, bound to the loop that opened it."""

    def __init__(
        self,
        dialect: Dialect,
        loop: asyncio.AbstractEventLoop,
        *,
        pg_pool: AsyncConnectionPool[AsyncConnection[Any]] | None = None,
        sqlite_pool: _SqlitePool | None = None,
    ) -> None:
        self._dialect = dialect
        self._loop = loop
        self._pg_pool = pg_pool
        self._sqlite_pool = sqlite_pool
        self._closed = False

    @classmethod
    async def open(
        cls, settings: DatabaseSettings, *, min_size: int = 1, max_size: int = 10
    ) -> Database:
        loop = asyncio.get_running_loop()
        check_event_loop(loop, settings.dialect)
        if settings.dialect is Dialect.SQLITE:
            if _sqlite_version_info() < _MIN_SQLITE:
                raise DatabaseError("SQLite 3.35 or later is required (RETURNING support)")
            if settings.sqlite_path is None:
                raise DatabaseError("SQLite needs a database path")
            pool = _SqlitePool(settings.sqlite_path, max(1, min(max_size, 4)))
            await pool.open()
            return cls(Dialect.SQLITE, loop, sqlite_pool=pool)

        if not settings.url:
            raise DatabaseError("PostgreSQL needs a database URL")
        from psycopg_pool import AsyncConnectionPool

        pg_pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool(
            settings.url,
            min_size=min_size,
            max_size=max_size,
            open=False,
            configure=register_vector,
            kwargs={
                "autocommit": True,
                "options": f"-c search_path={POSTGRES_SCHEMA},public -c TimeZone=UTC",
            },
        )
        await pg_pool.open(wait=True, timeout=30)
        return cls(Dialect.POSTGRESQL, loop, pg_pool=pg_pool)

    @property
    def dialect(self) -> Dialect:
        return self._dialect

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop that owns the connections."""
        return self._loop

    @property
    def pool(self) -> AsyncConnectionPool[AsyncConnection[Any]] | None:
        """The psycopg pool in server mode (shared with warden); None on SQLite."""
        return self._pg_pool

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pg_pool is not None:
            await self._pg_pool.close()
        if self._sqlite_pool is not None:
            await self._sqlite_pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """A connection for statements that each commit on their own."""
        if self._closed:
            raise DatabaseError("the database is closed")
        if self._sqlite_pool is not None:
            async with self._sqlite_pool.acquire() as raw:
                yield Connection(raw, self._dialect)
            return
        assert self._pg_pool is not None
        async with self._pg_pool.connection() as pg_raw:
            yield Connection(pg_raw, self._dialect)

    @asynccontextmanager
    async def transaction(self, conn: Connection | None = None) -> AsyncIterator[Connection]:
        """Run statements in one transaction; with ``conn``, join the caller's."""
        if conn is not None:
            async with conn._transaction():
                yield conn
            return
        async with self.connection() as fresh:
            async with fresh._transaction():
                yield fresh

    async def fetch_one(self, sql: str, params: Params = ()) -> Row | None:
        async with self.connection() as conn:
            return await conn.fetch_one(sql, params)

    async def fetch_all(self, sql: str, params: Params = ()) -> list[Row]:
        async with self.connection() as conn:
            return await conn.fetch_all(sql, params)

    async def fetch_value(self, sql: str, params: Params = ()) -> Any:
        async with self.connection() as conn:
            return await conn.fetch_value(sql, params)

    async def execute(self, sql: str, params: Params = ()) -> int:
        async with self.connection() as conn:
            return await conn.execute(sql, params)

    async def execute_many(self, sql: str, params_seq: Iterable[Params]) -> None:
        async with self.transaction() as conn:
            await conn.execute_many(sql, params_seq)
