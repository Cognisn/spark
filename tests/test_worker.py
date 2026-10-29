"""Tests for the worker role (``spark worker``): refusals, registration, the advisory lock.

The single-active-worker tests run two genuinely parallel workers, each on its own
event loop thread with its own PostgreSQL connections, against a real PostgreSQL
(testcontainers). Fixtures are synthetic; no table is touched.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from spark import worker
from spark.db.loop import LoopThread
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

PG_URL = "postgresql://spark:spark-dev-only@db.example.com:5432/spark"


def _server(url: str = PG_URL) -> RuntimeSettings:
    return RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url=url))


def _personal(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    )


# -- refusals -----------------------------------------------------------------


class TestCheckRuntime:
    def test_server_mode_on_postgresql_is_accepted(self) -> None:
        worker.check_runtime(_server())

    def test_personal_mode_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(worker.WorkerRefused, match="server mode"):
            worker.check_runtime(_personal(tmp_path))

    def test_sqlite_is_refused_even_in_server_mode(self, tmp_path: Path) -> None:
        runtime = RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
        )
        with pytest.raises(worker.WorkerRefused, match="PostgreSQL"):
            worker.check_runtime(runtime)

    def test_postgresql_without_a_url_is_refused(self) -> None:
        runtime = RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url=None))
        with pytest.raises(worker.WorkerRefused, match="PostgreSQL"):
            worker.check_runtime(runtime)

    def test_the_worker_refuses_sqlite_settings(self, tmp_path: Path) -> None:
        with pytest.raises(worker.WorkerRefused, match="PostgreSQL"):
            worker.Worker(
                DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db"),
                worker_id="w-sqlite",
            )


class TestMain:
    def test_personal_mode_exits_2_with_a_clear_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(worker, "_load_runtime", lambda: _personal(tmp_path))
        monkeypatch.setattr(worker, "_serve", _must_not_serve)
        assert worker.main() == 2
        err = capsys.readouterr().err
        assert "spark worker" in err
        assert "server mode" in err

    def test_a_database_behind_the_schema_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from spark.db.migrate import MigrationRequired

        def _behind(settings: DatabaseSettings) -> None:
            raise MigrationRequired("the database is behind; run `spark migrate`")

        monkeypatch.setattr(worker, "_load_runtime", lambda: _server())
        monkeypatch.setattr(worker, "_check_schema", _behind)
        monkeypatch.setattr(worker, "_serve", _must_not_serve)
        assert worker.main() == 2
        assert "spark migrate" in capsys.readouterr().err

    def test_server_mode_serves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        served: list[RuntimeSettings] = []
        monkeypatch.setattr(worker, "_load_runtime", lambda: _server())
        monkeypatch.setattr(worker, "_check_schema", lambda settings: None)
        monkeypatch.setattr(worker, "_serve", lambda runtime: served.append(runtime))
        assert worker.main() == 0
        assert served == [_server()]


def _must_not_serve(runtime: RuntimeSettings) -> None:
    pytest.fail("the worker must not start")


# -- registration -------------------------------------------------------------


@pytest.fixture
def clean_registry() -> Iterator[None]:
    worker.clear_registrations()
    yield
    worker.clear_registrations()


async def _noop(ctx: worker.WorkerContext) -> None:
    return None


@asynccontextmanager
async def _noop_service(ctx: worker.WorkerContext) -> AsyncIterator[None]:
    yield


class TestRegistration:
    def test_periodic_jobs_are_registered_in_order(self, clean_registry: None) -> None:
        worker.register_periodic("prune-a", 60, _noop)
        worker.register_periodic("prune-b", 3600, _noop)
        jobs = worker.registered_periodic()
        assert [(j.name, j.every_seconds) for j in jobs] == [("prune-a", 60), ("prune-b", 3600)]

    def test_a_duplicate_name_is_refused(self, clean_registry: None) -> None:
        worker.register_periodic("prune", 60, _noop)
        with pytest.raises(ValueError, match="prune"):
            worker.register_periodic("prune", 30, _noop)

    def test_a_non_positive_interval_is_refused(self, clean_registry: None) -> None:
        with pytest.raises(ValueError):
            worker.register_periodic("never", 0, _noop)

    def test_services_are_registered(self, clean_registry: None) -> None:
        worker.register_service("warden-pruning", _noop_service)
        assert [s.name for s in worker.registered_services()] == ["warden-pruning"]
        with pytest.raises(ValueError, match="warden-pruning"):
            worker.register_service("warden-pruning", _noop_service)

    def test_default_duties_include_scheduled_actions_and_registrations(
        self, clean_registry: None
    ) -> None:
        worker.register_service("warden-pruning", _noop_service)
        worker.register_periodic("audit-purge", 86400, _noop)
        services, periodic = worker.default_duties()
        assert [s.name for s in services] == ["scheduled-actions", "warden-pruning"]
        assert [p.name for p in periodic] == ["audit-purge"]


# -- the advisory lock and two parallel workers (PostgreSQL) -----------------


class Recorder:
    """Thread-safe record of when each worker's duties start and stop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, str, float]] = []
        self.active: set[str] = set()
        self.max_active = 0
        self.ticks: dict[str, int] = {}

    def service(self, ctx: worker.WorkerContext) -> Any:
        @asynccontextmanager
        async def _run() -> AsyncIterator[None]:
            with self._lock:
                self.active.add(ctx.worker_id)
                self.max_active = max(self.max_active, len(self.active))
                self.events.append(("start", ctx.worker_id, time.monotonic()))
            try:
                yield
            finally:
                with self._lock:
                    self.active.discard(ctx.worker_id)
                    self.events.append(("stop", ctx.worker_id, time.monotonic()))

        return _run()

    async def tick(self, ctx: worker.WorkerContext) -> None:
        with self._lock:
            self.ticks[ctx.worker_id] = self.ticks.get(ctx.worker_id, 0) + 1

    def kinds(self) -> list[tuple[str, str]]:
        with self._lock:
            return [(kind, who) for kind, who, _ in self.events]

    def is_active(self, worker_id: str) -> bool:
        with self._lock:
            return worker_id in self.active


class RunningWorker:
    """A worker on its own event loop thread, stoppable from the test thread."""

    def __init__(self, url: str, worker_id: str, recorder: Recorder) -> None:
        self.worker_id = worker_id
        self._thread = LoopThread(name=f"test-{worker_id}")
        self._loop = self._thread.start()
        self.worker = worker.Worker(
            DatabaseSettings(Dialect.POSTGRESQL, url=url),
            worker_id=worker_id,
            services=[worker.Service("record", recorder.service)],
            periodic=[worker.PeriodicJob("tick", 0.05, recorder.tick)],
            standby_seconds=0.1,
            check_seconds=0.1,
            takeover_grace=0.5,
        )
        self._stop: asyncio.Event = self._thread.run(_make_event())
        self._future = asyncio.run_coroutine_threadsafe(self.worker.run(self._stop), self._loop)
        self._stopped = False

    def stop(self, timeout: float = 20.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._future.result(timeout)
        finally:
            self._thread.stop()

    def lock_backend_pid(self) -> int | None:
        return self.worker.lock_backend_pid


async def _make_event() -> asyncio.Event:
    return asyncio.Event()


def _wait_for(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.postgres
class TestAdvisoryLock:
    async def test_only_one_holder_at_a_time(self, pg_url: str) -> None:
        first = worker.AdvisoryLock(pg_url)
        second = worker.AdvisoryLock(pg_url)
        try:
            assert await first.try_acquire() is True
            assert await second.try_acquire() is False
            assert await first.still_held() is True
            await first.release()
            assert await second.try_acquire() is True
        finally:
            await first.release()
            await second.release()

    async def test_the_lock_is_per_database(self, pg_server: str, pg_url: str) -> None:
        import uuid

        import psycopg

        other_name = f"t_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{other_name}"')
        other_url = pg_url.rpartition("/")[0] + f"/{other_name}"
        first = worker.AdvisoryLock(pg_url)
        second = worker.AdvisoryLock(other_url)
        try:
            assert await first.try_acquire() is True
            assert await second.try_acquire() is True
        finally:
            await first.release()
            await second.release()
            with psycopg.connect(pg_server, autocommit=True) as admin:
                admin.execute(f'DROP DATABASE IF EXISTS "{other_name}" WITH (FORCE)')

    async def test_a_lost_connection_is_a_lost_lock(self, pg_url: str) -> None:
        import psycopg

        first = worker.AdvisoryLock(pg_url)
        second = worker.AdvisoryLock(pg_url)
        try:
            assert await first.try_acquire() is True
            pid = first.backend_pid
            assert pid is not None
            async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as admin:
                await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            assert await first.still_held() is False
            assert await second.try_acquire() is True
        finally:
            await first.release()
            await second.release()


@pytest.mark.postgres
@pytest.mark.timeout(120)
class TestSingleActiveWorker:
    def test_the_second_worker_stands_by_and_takes_over(self, pg_url: str) -> None:
        recorder = Recorder()
        first = RunningWorker(pg_url, "worker-a", recorder)
        second: RunningWorker | None = None
        try:
            assert _wait_for(lambda: recorder.is_active("worker-a"))
            second = RunningWorker(pg_url, "worker-b", recorder)
            # Many standby rounds and lock checks pass; the second never becomes active.
            time.sleep(1.5)
            assert recorder.active == {"worker-a"}
            assert recorder.ticks.get("worker-b", 0) == 0
            assert recorder.ticks.get("worker-a", 0) > 0

            first.stop()
            assert _wait_for(lambda: recorder.is_active("worker-b"))
        finally:
            if second is not None:
                second.stop()
            first.stop()

        assert recorder.max_active == 1
        starts = [(who, at) for kind, who, at in recorder.events if kind == "start"]
        stops = {who: at for kind, who, at in recorder.events if kind == "stop"}
        assert [who for who, _ in starts] == ["worker-a", "worker-b"]
        assert stops["worker-a"] <= starts[1][1]

    def test_a_worker_whose_lock_connection_dies_steps_down(self, pg_url: str) -> None:
        import psycopg

        recorder = Recorder()
        first = RunningWorker(pg_url, "worker-a", recorder)
        second: RunningWorker | None = None
        try:
            assert _wait_for(lambda: recorder.is_active("worker-a"))
            second = RunningWorker(pg_url, "worker-b", recorder)
            time.sleep(0.5)
            pid = first.lock_backend_pid()
            assert pid is not None
            with psycopg.connect(pg_url, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            # The first steps down; afterwards exactly one worker (either) is active.
            assert _wait_for(lambda: ("stop", "worker-a") in recorder.kinds())
            assert _wait_for(lambda: len(recorder.active) == 1)
        finally:
            if second is not None:
                second.stop()
            first.stop()
        assert recorder.max_active == 1
