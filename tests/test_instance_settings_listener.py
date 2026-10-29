"""Propagation between web replicas over PostgreSQL LISTEN/NOTIFY (A8).

Each replica is a genuinely separate :class:`spark.db.Database` (its own pool)
with its own :class:`ServerSettings` and its own listener connection.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from spark.db import Database, migrate
from spark.instance_settings import listener as listener_mod
from spark.instance_settings.accessor import ServerSettings
from spark.instance_settings.listener import SettingsListener, start_listener, stop_listener
from spark.mode import DatabaseSettings, Dialect

pytestmark = pytest.mark.postgres

BOUND_SECONDS = 5.0


class NoKonfig:
    def get(self, key: str, default: Any = None) -> Any:
        return default


@dataclass
class Replica:
    db: Database
    settings: ServerSettings
    listener: SettingsListener


@pytest.fixture
def pg_settings(pg_url: str) -> DatabaseSettings:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
    migrate.upgrade(settings)
    return settings


async def _replica(settings: DatabaseSettings, **kwargs: Any) -> Replica:
    db = await Database.open(settings)
    source = ServerSettings(db, NoKonfig())
    started = await start_listener(source, settings, **kwargs)
    return Replica(db, source, started)


async def _close(replica: Replica) -> None:
    await stop_listener(replica.listener)
    await replica.db.close()


@pytest.fixture
async def replicas(pg_settings: DatabaseSettings) -> AsyncIterator[tuple[Replica, Replica]]:
    a = await _replica(pg_settings, idle_timeout=0.5)
    b = await _replica(pg_settings, idle_timeout=0.5)
    try:
        yield a, b
    finally:
        await _close(a)
        await _close(b)


async def _eventually(check: Callable[[], bool], within: float = BOUND_SECONDS) -> float:
    started = time.monotonic()
    while not check():
        if time.monotonic() - started > within:
            raise AssertionError(f"not propagated within {within}s")
        await asyncio.sleep(0.02)
    return time.monotonic() - started


async def _terminate_listeners(pg_url: str) -> int:
    import psycopg

    async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as admin:
        cur = await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = %s AND datname = current_database()",
            (listener_mod.APPLICATION_NAME,),
        )
        return len(await cur.fetchall())


class TestPropagation:
    async def test_a_change_on_one_replica_reaches_the_other_within_the_bound(
        self, replicas: tuple[Replica, Replica]
    ) -> None:
        a, b = replicas
        assert a.db is not b.db and a.db.pool is not b.db.pool
        assert b.settings.get("voice.engine") == "browser"
        await a.settings.set("voice.engine", "elevenlabs", actor="admin")
        elapsed = await _eventually(lambda: b.settings.get("voice.engine") == "elevenlabs")
        assert elapsed < BOUND_SECONDS

    async def test_clear_propagates(self, replicas: tuple[Replica, Replica]) -> None:
        a, b = replicas
        await a.settings.set("models.offered", ["m-1"], actor="admin")
        await _eventually(lambda: b.settings.get("models.offered") == ["m-1"])
        await a.settings.clear("models.offered", actor="admin")
        await _eventually(lambda: b.settings.get("models.offered") == [])

    async def test_changes_flow_both_ways(self, replicas: tuple[Replica, Replica]) -> None:
        a, b = replicas
        await b.settings.set("voice.engine", "elevenlabs", actor="admin")
        await _eventually(lambda: a.settings.get("voice.engine") == "elevenlabs")

    async def test_listener_starts_with_a_full_load(self, pg_settings: DatabaseSettings) -> None:
        writer = await Database.open(pg_settings)
        try:
            from spark.instance_settings import repository

            await repository.save_instance(writer, "voice.engine", "elevenlabs", actor="a")
            late = await _replica(pg_settings)
            try:
                assert late.settings.get("voice.engine") == "elevenlabs"
            finally:
                await _close(late)
        finally:
            await writer.close()


class TestReconnect:
    async def test_dropped_connection_reconnects_and_reloads_in_full(
        self, pg_settings: DatabaseSettings, pg_url: str
    ) -> None:
        """A change made while the listener is down is picked up by the reload on reconnect."""
        gate = asyncio.Event()
        gate.set()
        real_connect = listener_mod.default_connect

        async def gated_connect(url: str) -> Any:
            await gate.wait()
            return await real_connect(url)

        a = await _replica(pg_settings, idle_timeout=0.5)
        b = await _replica(
            pg_settings, idle_timeout=0.5, reconnect_delay=0.05, connect=gated_connect
        )
        try:
            gate.clear()  # hold b's next connection attempt
            assert await _terminate_listeners(pg_url) >= 2
            await _eventually(lambda: not b.listener.connected.is_set())
            # While b has no listening connection, the NOTIFY reaches nobody on b.
            await a.settings.set("voice.engine", "elevenlabs", actor="admin")
            await asyncio.sleep(0.3)
            assert b.settings.get("voice.engine") == "browser"
            gate.set()
            await _eventually(lambda: b.settings.get("voice.engine") == "elevenlabs")
            assert b.listener.reconnects >= 1
            await _eventually(lambda: a.listener.connected.is_set())
            # And notifications flow again after the reconnect.
            await a.settings.set("voice.engine", "browser", actor="admin")
            await _eventually(lambda: b.settings.get("voice.engine") == "browser")
        finally:
            await _close(a)
            await _close(b)

    async def test_failed_connection_attempts_back_off_and_retry(
        self, pg_settings: DatabaseSettings
    ) -> None:
        attempts: list[float] = []
        real_connect = listener_mod.default_connect

        async def flaky(url: str) -> Any:
            attempts.append(time.monotonic())
            if len(attempts) < 3:
                raise OSError("connection refused")
            return await real_connect(url)

        replica = await _replica(pg_settings, reconnect_delay=0.05, connect=flaky)
        try:
            assert replica.listener.connected.is_set()
            assert len(attempts) == 3
            assert attempts[2] - attempts[1] >= attempts[1] - attempts[0]
        finally:
            await _close(replica)


class TestStop:
    async def test_stop_closes_the_connection_and_stops_reloading(
        self, pg_settings: DatabaseSettings, pg_url: str
    ) -> None:
        a = await _replica(pg_settings, idle_timeout=0.5)
        b = await _replica(pg_settings, idle_timeout=0.5)
        try:
            await stop_listener(b.listener)
            assert not b.listener.connected.is_set()
            await a.settings.set("voice.engine", "elevenlabs", actor="admin")
            await asyncio.sleep(0.5)
            assert b.settings.get("voice.engine") == "browser"
            assert await _terminate_listeners(pg_url) == 1  # only a's remains
        finally:
            await _close(a)
            await b.db.close()

    async def test_stop_is_idempotent(self, pg_settings: DatabaseSettings) -> None:
        replica = await _replica(pg_settings)
        await stop_listener(replica.listener)
        await stop_listener(replica.listener)
        await replica.db.close()


class TestStartRefusals:
    async def test_needs_a_postgres_url(self) -> None:
        async def reload() -> None:
            return None

        with pytest.raises(ValueError):
            SettingsListener("", reload)

    async def test_start_waits_for_the_first_connection(
        self, pg_settings: DatabaseSettings
    ) -> None:
        async def never(url: str) -> Awaitable[Any]:
            raise OSError("down")

        db = await Database.open(pg_settings)
        try:
            source = ServerSettings(db, NoKonfig())
            with pytest.raises(TimeoutError):
                await start_listener(
                    source, pg_settings, connect=never, reconnect_delay=0.05, wait=0.3
                )
        finally:
            await db.close()
