"""Tests for the sync bridge (spark.db.bridge) and the loop thread (spark.db.loop).

Synchronous orchestrators run on worker threads and reach the async database
only through SyncDatabase. These tests use real threads and a real loop thread.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import time
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from spark.db.bridge import BridgeError, SyncDatabase
from spark.db.engine import Database, Executor
from spark.db.loop import LoopThread, new_event_loop
from spark.mode import DatabaseSettings, Dialect


async def _count(db: Executor) -> int:
    return int(await db.fetch_value("SELECT COUNT(*) FROM items"))


async def _add(db: Executor, name: str) -> int:
    return int(await db.fetch_value("INSERT INTO items (name) VALUES (?) RETURNING id", (name,)))


async def _name(db: Executor, item_id: int) -> str | None:
    return await db.fetch_value("SELECT name FROM items WHERE id = ?", (item_id,))  # type: ignore[no-any-return]


async def _fail(db: Executor) -> None:
    raise LookupError("not here")


async def _slow(db: Executor, seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "late"


def _open(loop_thread: LoopThread, settings: DatabaseSettings) -> Database:
    async def go() -> Database:
        db = await Database.open(settings, max_size=8)
        await db.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)"
            if db.dialect is Dialect.SQLITE
            else "CREATE TABLE items (id SERIAL PRIMARY KEY, name TEXT NOT NULL)"
        )
        return db

    return loop_thread.run(go())


@pytest.fixture
def loop_thread() -> Iterator[LoopThread]:
    lt = LoopThread(name="test-db-loop")
    lt.start()
    try:
        yield lt
    finally:
        lt.stop()


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def sync_db(
    request: pytest.FixtureRequest, loop_thread: LoopThread, tmp_path: Path
) -> Iterator[SyncDatabase]:
    if request.param == "sqlite":
        settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "b.db")
    else:
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=request.getfixturevalue("pg_url"))
    db = _open(loop_thread, settings)
    try:
        yield SyncDatabase(db)
    finally:
        loop_thread.run(db.close())


class TestCall:
    def test_runs_a_repository_function_from_a_worker_thread(self, sync_db: SyncDatabase):
        new_id = sync_db.call(_add, "alpha")
        assert sync_db.call(_name, new_id) == "alpha"
        assert sync_db.call(_count) == 1

    def test_exceptions_propagate_with_their_type(self, sync_db: SyncDatabase):
        with pytest.raises(LookupError, match="not here"):
            sync_db.call(_fail)

    def test_timeout_raises_and_cancels(self, loop_thread: LoopThread, tmp_path: Path):
        db = _open(loop_thread, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "t.db"))
        try:
            bridge = SyncDatabase(db, timeout=0.2)
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                bridge.call(_slow, 5.0)
            assert time.monotonic() - started < 2.0
        finally:
            loop_thread.run(db.close())

    def test_raw_statement_helpers(self, sync_db: SyncDatabase):
        sync_db.execute("INSERT INTO items (name) VALUES (?)", ("x",))
        assert sync_db.fetch_value("SELECT COUNT(*) FROM items") == 1
        row = sync_db.fetch_one("SELECT name FROM items")
        assert row == {"name": "x"}
        assert sync_db.fetch_all("SELECT name FROM items") == [{"name": "x"}]


class TestDeadlockGuard:
    def test_refuses_on_the_owning_loop_thread(
        self, sync_db: SyncDatabase, loop_thread: LoopThread
    ):
        """Blocking the loop that must run the coroutine would hang for ever."""

        async def on_the_loop() -> str:
            try:
                sync_db.call(_count)
            except BridgeError as exc:
                return str(exc)
            return "no error"

        message = loop_thread.run(on_the_loop())
        assert "event loop thread" in message

    def test_refusal_does_not_leak_an_unawaited_coroutine(
        self, sync_db: SyncDatabase, loop_thread: LoopThread
    ):
        async def on_the_loop() -> None:
            with pytest.raises(BridgeError):
                sync_db.call(_count)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            loop_thread.run(on_the_loop())
            gc.collect()
        assert not [w for w in caught if "never awaited" in str(w.message)]

    def test_refuses_when_the_loop_is_stopped(self, tmp_path: Path):
        lt = LoopThread(name="short-lived")
        lt.start()
        db = _open(lt, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db"))
        lt.run(db.close())
        lt.stop()
        with pytest.raises(BridgeError, match="not running"):
            SyncDatabase(db).call(_count)


class TestParallelWorkers:
    def test_many_threads_at_once(self, sync_db: SyncDatabase):
        """16 threads released together, 25 round trips each, no lost or crossed results."""
        threads = 16
        per_thread = 25
        barrier = threading.Barrier(threads)

        def worker(n: int) -> list[tuple[str, str | None]]:
            barrier.wait()
            out = []
            for i in range(per_thread):
                name = f"t{n}-{i}"
                new_id = sync_db.call(_add, name)
                out.append((name, sync_db.call(_name, new_id)))
            return out

        with ThreadPoolExecutor(max_workers=threads) as pool:
            results = [
                r for f in [pool.submit(worker, n) for n in range(threads)] for r in f.result()
            ]
        assert len(results) == threads * per_thread
        assert all(written == read for written, read in results)
        assert sync_db.call(_count) == threads * per_thread

    def test_worker_threads_and_loop_tasks_together(
        self, sync_db: SyncDatabase, loop_thread: LoopThread
    ):
        """The 0.2.0 KG race: a background thread writes while requests on the loop read."""
        done = threading.Event()
        errors: list[BaseException] = []

        def builder() -> None:
            try:
                for i in range(100):
                    sync_db.call(_add, f"node{i}")
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                done.set()

        async def status_polls() -> int:
            polls = 0
            while not done.is_set():
                count = await _count(sync_db.database)
                assert count is not None
                polls += 1
                await asyncio.sleep(0)
            return polls

        thread = threading.Thread(target=builder)
        thread.start()
        polls = loop_thread.run(status_polls())
        thread.join()
        assert not errors
        assert polls > 0
        assert sync_db.call(_count) == 100


class TestLoopThread:
    def test_runs_coroutines_and_stops(self):
        lt = LoopThread(name="lt")
        loop = lt.start()
        assert lt.run(asyncio.sleep(0, result=7)) == 7
        assert loop.is_running()
        lt.stop()
        assert not loop.is_running()
        assert loop.is_closed()

    def test_start_twice_is_an_error(self):
        lt = LoopThread(name="lt2")
        lt.start()
        try:
            with pytest.raises(RuntimeError):
                lt.start()
        finally:
            lt.stop()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_uses_the_selector_loop_on_windows(self):
        loop = new_event_loop()
        try:
            assert isinstance(loop, asyncio.SelectorEventLoop)
        finally:
            loop.close()
