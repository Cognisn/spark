"""The worker role: ``spark worker``, server mode only.

The worker runs Spark's background duties away from the web processes: scheduled
actions today, and whatever later releases register (warden's pruning in 0.3.0).
Any number of workers may run against one database, but only one is active at a
time. The active worker holds a PostgreSQL session-level advisory lock on a
connection of its own; the others stand by, retrying every ``standby_seconds``,
and one of them takes over when the active worker stops or its lock connection
is lost (PostgreSQL releases a session lock when its connection ends).

A worker that takes the lock waits ``takeover_grace`` seconds (by default twice
the lock check interval, while confirming it still holds the lock) before it
starts its duties. A worker whose lock connection was cut notices within one
check interval and stops its duties, so the two do not overlap. Scheduled
actions also take a per-action lock in the database before they run.

Later releases add duties through the registration point, before the worker
starts (for example from a module imported at start-up):

* :func:`register_periodic` for a coroutine run every N seconds while active;
* :func:`register_service` for an async context manager entered when the worker
  becomes active and exited when it stops or steps down (warden's
  ``identity.pruning(every_seconds=...)`` has this shape).

Each duty receives a :class:`WorkerContext` with the worker's async
:class:`~spark.db.Database` (its ``pool`` is the psycopg pool for warden), the
:class:`~spark.db.SyncDatabase` bridge for synchronous code on other threads, and
the konfig application context.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from spark.db import Database, SyncDatabase

__all__ = [
    "WORKER_LOCK_KEY",
    "AdvisoryLock",
    "PeriodicJob",
    "Service",
    "Worker",
    "WorkerContext",
    "WorkerRefused",
    "check_runtime",
    "clear_registrations",
    "default_duties",
    "main",
    "register_periodic",
    "register_service",
    "registered_periodic",
    "registered_services",
]

logger = logging.getLogger(__name__)

# The advisory lock key for "the active worker": the bytes of "spark.wk" as a
# signed 64-bit integer. Advisory locks are per database, so this means one
# active worker per database.
WORKER_LOCK_KEY: Final = 0x737061726B2E776B

DEFAULT_STANDBY_SECONDS: Final = 5.0
DEFAULT_CHECK_SECONDS: Final = 5.0


class WorkerRefused(RuntimeError):
    """The worker cannot run with this configuration (personal mode or SQLite)."""


def check_runtime(runtime: RuntimeSettings) -> None:
    """Refuse to run outside server mode or without PostgreSQL."""
    if runtime.mode is not Mode.SERVER:
        raise WorkerRefused(
            "spark worker runs only in server mode (set mode to server, SPARK__MODE=server, "
            "with a PostgreSQL database.url); in personal mode the tray daemon runs "
            "scheduled actions"
        )
    _check_database(runtime.database)


def _check_database(settings: DatabaseSettings) -> None:
    if settings.dialect is not Dialect.POSTGRESQL or not settings.url:
        raise WorkerRefused(
            "spark worker needs PostgreSQL: the advisory lock that keeps one active "
            "worker per database does not exist on SQLite"
        )


# -- duties and the registration point ---------------------------------------


@dataclass(frozen=True)
class WorkerContext:
    """What every duty receives."""

    worker_id: str
    database: Database
    sync_db: SyncDatabase
    app: Any = None


PeriodicFn = Callable[[WorkerContext], Awaitable[None]]
ServiceFactory = Callable[[WorkerContext], AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class PeriodicJob:
    """A coroutine run every ``every_seconds`` while the worker is active."""

    name: str
    every_seconds: float
    run: PeriodicFn


@dataclass(frozen=True)
class Service:
    """An async context manager held open while the worker is active."""

    name: str
    factory: ServiceFactory


_periodic: dict[str, PeriodicJob] = {}
_services: dict[str, Service] = {}


def register_periodic(name: str, every_seconds: float, fn: PeriodicFn) -> None:
    """Run ``fn(ctx)`` every ``every_seconds`` on the active worker."""
    if every_seconds <= 0:
        raise ValueError(f"periodic job {name!r} needs a positive interval")
    if name in _periodic:
        raise ValueError(f"a periodic job named {name!r} is already registered")
    _periodic[name] = PeriodicJob(name, every_seconds, fn)


def register_service(name: str, factory: ServiceFactory) -> None:
    """Hold ``factory(ctx)`` open on the active worker."""
    if name in _services or name == _SCHEDULED_ACTIONS:
        raise ValueError(f"a service named {name!r} is already registered")
    _services[name] = Service(name, factory)


def registered_periodic() -> list[PeriodicJob]:
    return list(_periodic.values())


def registered_services() -> list[Service]:
    return list(_services.values())


def clear_registrations() -> None:
    """Forget every registration (tests only)."""
    _periodic.clear()
    _services.clear()


_SCHEDULED_ACTIONS: Final = "scheduled-actions"


@asynccontextmanager
async def _scheduled_actions(ctx: WorkerContext) -> AsyncIterator[None]:
    """Run the action scheduler (APScheduler threads) while active."""
    from spark.scheduler.runner import ActionRunner

    runner = ActionRunner(ctx.app, ctx.worker_id, ctx.sync_db)
    await asyncio.to_thread(runner.start)
    try:
        yield
    finally:
        await asyncio.to_thread(runner.stop)


def default_duties() -> tuple[list[Service], list[PeriodicJob]]:
    """The scheduled actions, then every registered service and periodic job."""
    services = [Service(_SCHEDULED_ACTIONS, _scheduled_actions), *registered_services()]
    return services, registered_periodic()


# -- the advisory lock --------------------------------------------------------


class AdvisoryLock:
    """A session-level PostgreSQL advisory lock on a dedicated connection."""

    def __init__(self, url: str, key: int = WORKER_LOCK_KEY) -> None:
        self._url = url
        self._key = key
        self._conn: AsyncConnection[Any] | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    @property
    def backend_pid(self) -> int | None:
        """The server process holding the lock's connection (None when not connected)."""
        if self._conn is None or self._conn.closed:
            return None
        return int(self._conn.info.backend_pid)

    async def try_acquire(self) -> bool:
        """Take the lock if it is free; never waits."""
        if self._held:
            return True
        try:
            conn = await self._connection()
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (self._key,))
            row = await cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - the database is down: stand by
            logger.warning("Worker lock: cannot reach the database (%s)", type(exc).__name__)
            await self._close()
            return False
        self._held = bool(row and row[0])
        return self._held

    async def still_held(self) -> bool:
        """Whether the lock connection is alive (so the lock is still ours)."""
        if not self._held or self._conn is None:
            return False
        try:
            await self._conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 - connection lost: so is the lock
            logger.warning("Worker lock: connection lost (%s)", type(exc).__name__)
            await self._close()
            return False
        return True

    async def release(self) -> None:
        """Release the lock (if held) and close the connection."""
        if self._held and self._conn is not None and not self._conn.closed:
            try:
                await self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
            except Exception as exc:  # noqa: BLE001 - closing releases it anyway
                logger.debug("Worker lock: unlock failed (%s)", type(exc).__name__)
        await self._close()

    async def _connection(self) -> AsyncConnection[Any]:
        if self._conn is None or self._conn.closed:
            import psycopg

            # TCP keepalives so a vanished peer ends the session (and frees the lock).
            self._conn = await psycopg.AsyncConnection.connect(
                self._url,
                autocommit=True,
                keepalives=1,
                keepalives_idle=10,
                keepalives_interval=5,
                keepalives_count=3,
                application_name="spark-worker",
            )
        return self._conn

    async def _close(self) -> None:
        self._held = False
        conn, self._conn = self._conn, None
        if conn is not None and not conn.closed:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass


# -- the worker ---------------------------------------------------------------


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


class Worker:
    """Stand by until the advisory lock is free, then run the duties while holding it."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        worker_id: str,
        app: Any = None,
        services: Sequence[Service] = (),
        periodic: Sequence[PeriodicJob] = (),
        standby_seconds: float = DEFAULT_STANDBY_SECONDS,
        check_seconds: float = DEFAULT_CHECK_SECONDS,
        takeover_grace: float | None = None,
        lock_key: int = WORKER_LOCK_KEY,
    ) -> None:
        _check_database(settings)
        assert settings.url is not None
        self._settings = settings
        self._worker_id = worker_id
        self._app = app
        self._services = list(services)
        self._periodic = list(periodic)
        self._standby_seconds = standby_seconds
        self._check_seconds = check_seconds
        self._takeover_grace = 2 * check_seconds if takeover_grace is None else takeover_grace
        self._lock = AdvisoryLock(settings.url, lock_key)

    @property
    def lock_backend_pid(self) -> int | None:
        return self._lock.backend_pid

    async def run(self, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set."""
        from spark.db import Database, SyncDatabase

        database = await Database.open(self._settings)
        ctx = WorkerContext(self._worker_id, database, SyncDatabase(database), self._app)
        standing_by = False
        try:
            while not stop.is_set():
                if not await self._lock.try_acquire():
                    if not standing_by:
                        logger.info(
                            "Worker %s standing by (another worker is active)", ctx.worker_id
                        )
                        standing_by = True
                    await _wait(stop, self._standby_seconds)
                    continue
                standing_by = False
                if not await self._hold_through_grace(stop):
                    await self._lock.release()
                    continue
                logger.info("Worker %s is active (holds the worker lock)", ctx.worker_id)
                try:
                    await self._active(ctx, stop)
                except Exception:
                    logger.exception("Worker %s: a duty failed; stepping down", ctx.worker_id)
                await self._lock.release()
                if not stop.is_set():
                    # Give a standby worker the first chance to take over.
                    await _wait(stop, self._standby_seconds)
        finally:
            await self._lock.release()
            await database.close()
            logger.info("Worker %s stopped", ctx.worker_id)

    async def _hold_through_grace(self, stop: asyncio.Event) -> bool:
        """Keep the lock for ``takeover_grace`` seconds before starting any duty."""
        deadline = asyncio.get_running_loop().time() + self._takeover_grace
        while not stop.is_set():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await _wait(stop, min(remaining, self._check_seconds))
            if not await self._lock.still_held():
                return False
        return False

    async def _active(self, ctx: WorkerContext, stop: asyncio.Event) -> None:
        async with AsyncExitStack() as stack:
            for service in self._services:
                logger.info("Worker %s: starting %s", ctx.worker_id, service.name)
                await stack.enter_async_context(service.factory(ctx))
            tasks = [
                asyncio.create_task(self._every(job, ctx), name=f"worker-{job.name}")
                for job in self._periodic
            ]
            try:
                while not stop.is_set():
                    await _wait(stop, self._check_seconds)
                    if stop.is_set():
                        break
                    if not await self._lock.still_held():
                        logger.warning(
                            "Worker %s lost the worker lock; stepping down", ctx.worker_id
                        )
                        break
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _every(self, job: PeriodicJob, ctx: WorkerContext) -> None:
        while True:
            try:
                await job.run(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one failed run never stops the job
                logger.error("Worker job %s failed (%s)", job.name, type(exc).__name__)
            await asyncio.sleep(job.every_seconds)


# -- ``spark worker`` ---------------------------------------------------------


def _load_runtime() -> RuntimeSettings:
    """Read the mode and database settings through konfig (no database access)."""
    return _with_app(lambda app: None)[0]


def _with_app(body: Callable[[Any], Any]) -> tuple[RuntimeSettings, Any]:
    import spark
    from spark.core import application

    async def _run() -> tuple[RuntimeSettings, Any]:
        from konfig import AppContext

        config_path = application._get_config_path()
        application._ensure_config(config_path)
        async with AppContext(
            name="Spark Worker",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=application._default_settings(),
            env_prefix="SPARK",
        ) as app:
            runtime = application.configure_runtime(app.settings)
            return runtime, await _maybe_await(body(app))

    return asyncio.run(_run())


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


def _check_schema(settings: DatabaseSettings) -> None:
    """Refuse a database that is not at the current schema (``spark migrate`` first)."""
    from spark.db import migrate

    migrate.check_at_head(settings)


def _worker_id() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


def _serve(runtime: RuntimeSettings) -> None:
    """Run the worker until SIGTERM or SIGINT."""

    async def _body(app: Any) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
        services, periodic = default_duties()
        settings = app.settings if app is not None else None
        worker = Worker(
            runtime.database,
            worker_id=_worker_id(),
            app=app,
            services=services,
            periodic=periodic,
            standby_seconds=_setting(settings, "worker.standby_seconds", DEFAULT_STANDBY_SECONDS),
            check_seconds=_setting(settings, "worker.check_seconds", DEFAULT_CHECK_SECONDS),
        )
        await worker.run(stop)

    _with_app(_body)


def _setting(settings: Any, key: str, default: float) -> float:
    if settings is None:
        return default
    try:
        return float(settings.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def main() -> int:
    """``spark worker``: refuse personal mode, SQLite or an old schema; then run."""
    from spark.db.migrate import MigrationError
    from spark.mode import ModeConfigError

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        runtime = _load_runtime()
        check_runtime(runtime)
        _check_schema(runtime.database)
    except (WorkerRefused, ModeConfigError, MigrationError) as exc:
        print(f"spark worker: {exc}", file=sys.stderr)
        return 2
    try:
        _serve(runtime)
    except KeyboardInterrupt:
        logger.info("Spark worker shut down by user")
    return 0
