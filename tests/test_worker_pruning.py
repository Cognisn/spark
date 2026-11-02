"""Server mode: the active worker prunes warden's data and installs SPARK's audit hooks.

``spark.identity.pruning.register`` adds three duties to the worker's
registration point, with intervals from settings: warden's identity pruning
(``identity.pruning(every_seconds=...)``) as a service, single sign-on pruning
as a periodic job, and, only when ``warden.audit.retention_days`` is set, the
audit retention purge. A real worker on a PostgreSQL clone at head runs each of
them with short intervals while it holds the worker lock, with SPARK's audit
hooks installed for as long as it is active.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from warden.audit import set_recorder
from warden.identity import Identity
from warden.identity.sso.service import SingleSignOn

from spark import mode, worker
from spark.identity import audit as spark_audit
from spark.identity import pruning
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.test_identity_runtime import Konfig
from tests.test_instance_settings_web import MemorySecrets

pytestmark = pytest.mark.postgres

PUBLIC = "https://spark.example.com"


@pytest.fixture
def server_db(pg_server: str, _pg_template: str) -> Iterator[DatabaseSettings]:
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=f"{base}/{name}")
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def clean_registrations() -> Iterator[None]:
    worker.clear_registrations()
    previous = set_recorder(None)
    yield
    worker.clear_registrations()
    set_recorder(previous)


def _konfig(**extra: Any) -> Konfig:
    values: dict[str, Any] = {"server": {"public_base_url": PUBLIC}, "warden": {}}
    for dotted, value in extra.items():
        node = values
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return Konfig(values)


class App:
    def __init__(self, settings: Konfig) -> None:
        self.settings = settings
        self.secrets = MemorySecrets()


class TestRegistration:
    def test_identity_and_sso_pruning_are_registered_with_the_configured_interval(self) -> None:
        pruning.register(_konfig(worker__identity_prune_seconds=120))
        assert [s.name for s in worker.registered_services()] == [pruning.IDENTITY_PRUNING]
        [job] = worker.registered_periodic()
        assert (job.name, job.every_seconds) == (pruning.SSO_PRUNING, 120.0)

    def test_the_default_interval_is_an_hour(self) -> None:
        pruning.register(_konfig())
        [job] = worker.registered_periodic()
        assert job.every_seconds == 3600.0

    def test_the_audit_purge_is_registered_only_with_a_retention(self) -> None:
        pruning.register(_konfig(warden__audit__retention_days=400, worker__audit_purge_seconds=60))
        jobs = {job.name: job.every_seconds for job in worker.registered_periodic()}
        assert jobs[pruning.AUDIT_PURGE] == 60.0

    def test_registering_twice_is_harmless(self) -> None:
        pruning.register(_konfig())
        pruning.register(_konfig())
        assert len(worker.registered_services()) == 1


class TestTheWorkerRunsThem:
    async def test_an_active_worker_prunes_and_audits(
        self, server_db: DatabaseSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, server_db))
        calls: list[str] = []
        audit_installed: list[bool] = []
        real_prune = Identity.prune
        real_sso_prune = SingleSignOn.prune
        real_purge = pruning.purge

        async def prune(self: Identity) -> Any:
            calls.append("identity")
            audit_installed.append(spark_audit.current() is not None)
            return await real_prune(self)

        async def sso_prune(self: SingleSignOn) -> int:
            calls.append("sso")
            return await real_sso_prune(self)

        async def purge(*args: Any, **kwargs: Any) -> Any:
            calls.append("purge")
            return await real_purge(*args, **kwargs)

        monkeypatch.setattr(Identity, "prune", prune)
        monkeypatch.setattr(SingleSignOn, "prune", sso_prune)
        monkeypatch.setattr(pruning, "purge", purge)
        monkeypatch.setattr("spark.core.application._get_data_path", lambda: tmp_path)

        settings = _konfig(
            worker__identity_prune_seconds=0.2,
            worker__audit_purge_seconds=0.2,
            warden__audit__retention_days=400,
        )
        pruning.register(settings)
        services, periodic = worker.default_duties()
        # The scheduled actions are another test's concern.
        services = [s for s in services if s.name != "scheduled-actions"]
        running = worker.Worker(
            server_db,
            worker_id="worker-prune",
            app=App(settings),
            services=services,
            periodic=periodic,
            standby_seconds=0.1,
            check_seconds=0.1,
            takeover_grace=0.1,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(running.run(stop))
        try:
            for _ in range(200):
                if {"identity", "sso", "purge"} <= set(calls) and calls.count("sso") >= 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, 20)
            mode.reset()
        assert {"identity", "sso", "purge"} <= set(calls), calls
        assert calls.count("sso") >= 2, calls
        assert audit_installed and all(audit_installed)
        # Stepping down uninstalls SPARK's audit hooks.
        assert spark_audit.current() is None


class TestServeRegistersPruning:
    def test_spark_worker_registers_wardens_pruning_before_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        class FakeWorker:
            def __init__(self, settings: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def run(self, stop: asyncio.Event) -> None:
                return None

        def with_app(body: Any) -> tuple[Any, Any]:
            return None, asyncio.run(body(App(_konfig(warden__audit__retention_days=400))))

        monkeypatch.setattr(worker, "Worker", FakeWorker)
        monkeypatch.setattr(worker, "_with_app", with_app)
        runtime = RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y")
        )
        worker._serve(runtime)
        assert pruning.IDENTITY_PRUNING in [s.name for s in captured["services"]]
        names = [job.name for job in captured["periodic"]]
        assert pruning.SSO_PRUNING in names and pruning.AUDIT_PURGE in names
