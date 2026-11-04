"""Server mode with restricted database roles (Q31, Q32).

A deployment that wants warden's audit trail append-only at the database level
keeps the schema owner for ``spark migrate`` and gives web and worker their own
login roles, which own nothing (a table's owner bypasses grants). These tests
build those roles on a PostgreSQL clone at head, as docs/deployment/database.md
describes, and prove:

* start-up needs no ``CREATE`` on the database: web's check
  (``migrate.prepare(auto_migrate=False)``) and the worker's schema check pass
  as roles that hold only ``CONNECT`` there, and ``check_at_head`` creates
  nothing, even as the owner;
* ``spark migrate`` as the owner still creates both schemas on an empty database;
* with ``worker.audit_purge_database_url`` set, the worker's audit purge
  connects as the purger role and deletes the purged prefix there, while the
  worker's own role holds no ``DELETE`` on ``warden.audit_events``; unset, the
  purge runs on the worker's own connection as before.

The settings parsing for the purge URL is tested without PostgreSQL.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from warden.audit import AuditEvent
from warden.audit import grants as warden_grants
from warden.audit import retention as warden_retention
from warden.audit import set_recorder

from spark import mode, worker
from spark.db import Database, SyncDatabase, migrate
from spark.identity import audit as spark_audit
from spark.identity import migrations as identity_migrations
from spark.identity import pruning
from spark.identity.runtime import IdentityRuntime
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.test_identity_runtime import Konfig
from tests.test_instance_settings_web import MemorySecrets

PUBLIC = "https://spark.example.com"
PURGE_KEY = "worker.audit_purge_database_url"

# Fixed sandbox values; nothing here is a real credential.
WEB_PASSWORD = "web-test-dev-only"  # noqa: S105
WORKER_PASSWORD = "worker-test-dev-only"  # noqa: S105
PURGER_PASSWORD = "purger-test-dev-only"  # noqa: S105

# What docs/deployment/database.md grants on Spark's own schema (step 2), with no
# CREATE on the database.
_SPARK_GRANTS = """
GRANT CONNECT ON DATABASE "{db}" TO {roles};
GRANT USAGE ON SCHEMA spark TO {roles};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA spark TO {roles};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA spark TO {roles};
REVOKE INSERT, UPDATE, DELETE ON spark.alembic_version FROM {roles};
"""


def _as_role(url: str, role: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{role}:{password}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


@dataclass(frozen=True)
class Roles:
    owner: DatabaseSettings
    web: DatabaseSettings
    worker: DatabaseSettings
    purger: DatabaseSettings
    web_role: str
    worker_role: str
    purger_role: str


def _create_roles(owner_url: str, *, with_grants: bool) -> tuple[list[str], dict[str, str]]:
    suffix = uuid.uuid4().hex[:10]
    names = {"web": f"r{suffix}_web", "worker": f"r{suffix}_worker", "purger": f"r{suffix}_purger"}
    passwords = {"web": WEB_PASSWORD, "worker": WORKER_PASSWORD, "purger": PURGER_PASSWORD}
    db = _database_name(owner_url)
    with psycopg.connect(owner_url, autocommit=True) as conn:
        for key, name in names.items():
            conn.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{passwords[key]}'")
        conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {", ".join(names.values())}')
        if with_grants:
            app_roles = f"{names['web']}, {names['worker']}"
            conn.execute(_SPARK_GRANTS.format(db=db, roles=app_roles))
            for app in ("web", "worker"):
                conn.execute(warden_grants.grants_sql(app_role=names[app]))
            conn.execute(_purger_only(names["purger"]))
    return list(names.values()), {k: _as_role(owner_url, n, passwords[k]) for k, n in names.items()}


def _purger_only(role: str) -> str:
    """warden's purger block, without an application block (which would need another role)."""
    sql = warden_grants.grants_sql(app_role="__app__", purger_role=role)
    blocks = sql.split(";\n")
    # The application block names the placeholder role; keep the purger's statements.
    return ";\n".join(b for b in blocks if "__app__" not in b and b.strip()) + ";\n"


def _drop_roles(owner_url: str, server_url: str, names: list[str]) -> None:
    with psycopg.connect(owner_url, autocommit=True) as conn:
        for name in names:
            conn.execute(f"DROP OWNED BY {name}")
    with psycopg.connect(server_url, autocommit=True) as conn:
        for name in names:
            conn.execute(f'REVOKE ALL ON DATABASE "{_database_name(owner_url)}" FROM {name}')
            conn.execute(f"DROP ROLE IF EXISTS {name}")


@pytest.fixture
def owner_db(pg_server: str, _pg_template: str) -> Iterator[str]:
    """A clone of the session's database at head (both chains), as the owner's URL."""
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield f"{base}/{name}"
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def roles(pg_server: str, owner_db: str) -> Iterator[Roles]:
    names, urls = _create_roles(owner_db, with_grants=True)
    try:
        yield Roles(
            owner=DatabaseSettings(Dialect.POSTGRESQL, url=owner_db),
            web=DatabaseSettings(Dialect.POSTGRESQL, url=urls["web"]),
            worker=DatabaseSettings(Dialect.POSTGRESQL, url=urls["worker"]),
            purger=DatabaseSettings(Dialect.POSTGRESQL, url=urls["purger"]),
            web_role=names[0],
            worker_role=names[1],
            purger_role=names[2],
        )
    finally:
        _drop_roles(owner_db, pg_server, names)


@pytest.fixture
def empty_with_roles(pg_server: str, pg_url: str) -> Iterator[Roles]:
    """An empty database (no schemas yet) and login roles holding only CONNECT on it."""
    names, urls = _create_roles(pg_url, with_grants=False)
    try:
        yield Roles(
            owner=DatabaseSettings(Dialect.POSTGRESQL, url=pg_url),
            web=DatabaseSettings(Dialect.POSTGRESQL, url=urls["web"]),
            worker=DatabaseSettings(Dialect.POSTGRESQL, url=urls["worker"]),
            purger=DatabaseSettings(Dialect.POSTGRESQL, url=urls["purger"]),
            web_role=names[0],
            worker_role=names[1],
            purger_role=names[2],
        )
    finally:
        _drop_roles(pg_url, pg_server, names)


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    migrate.clear_chains()
    worker.clear_registrations()
    previous = set_recorder(None)
    yield
    migrate.clear_chains()
    worker.clear_registrations()
    set_recorder(previous)
    mode.reset()


def _privilege(owner: DatabaseSettings, sql: str, *params: Any) -> Any:
    assert owner.url is not None
    with psycopg.connect(owner.url, autocommit=True) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _schemas(owner: DatabaseSettings) -> set[str]:
    assert owner.url is not None
    with psycopg.connect(owner.url, autocommit=True) as conn:
        rows = conn.execute("SELECT nspname FROM pg_namespace").fetchall()
    return {r[0] for r in rows}


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


# -- Q31: start-up without CREATE on the database ------------------------------


@pytest.mark.postgres
class TestStartUpWithoutCreate:
    def test_the_roles_hold_no_create_on_the_database(self, roles: Roles) -> None:
        db = _database_name(roles.owner.url or "")
        for role in (roles.web_role, roles.worker_role):
            assert (
                _privilege(roles.owner, "SELECT has_database_privilege(%s, %s, 'CREATE')", role, db)
                is False
            )

    def test_the_web_start_up_check_passes_as_the_web_role(self, roles: Roles) -> None:
        identity_migrations.register()
        migrate.prepare(roles.web, auto_migrate=False)

    def test_the_workers_schema_check_passes_as_the_worker_role(self, roles: Roles) -> None:
        worker._check_schema(roles.worker)

    async def test_the_web_role_opens_the_database_and_starts_warden(self, roles: Roles) -> None:
        identity_migrations.register()
        await asyncio.to_thread(migrate.prepare, roles.web, auto_migrate=False)
        mode.configure(RuntimeSettings(Mode.SERVER, roles.web))
        database = await Database.open(roles.web)
        try:
            app = App(_konfig())
            runtime = await IdentityRuntime.start(database, app.settings, app.secrets)
            runtime.close()
            assert await database.fetch_value("SELECT current_user") == roles.web_role
        finally:
            await database.close()

    def test_a_restricted_role_on_an_empty_database_is_told_to_migrate(
        self, empty_with_roles: Roles
    ) -> None:
        identity_migrations.register()
        with pytest.raises(migrate.MigrationRequired, match="spark migrate"):
            migrate.check_at_head(empty_with_roles.web)
        assert "spark" not in _schemas(empty_with_roles.owner)

    def test_check_at_head_creates_no_schema_even_as_the_owner(self, pg_url: str) -> None:
        owner = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        with pytest.raises(migrate.MigrationRequired):
            migrate.check_at_head(owner)
        with pytest.raises(migrate.MigrationRequired):
            migrate.prepare(owner, auto_migrate=False)
        assert "spark" not in _schemas(owner)

    def test_spark_migrate_as_the_owner_creates_both_schemas_on_an_empty_database(
        self, pg_url: str
    ) -> None:
        owner = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        identity_migrations.register()
        results = migrate.migrate_all(owner)
        assert results["spark"] == migrate.head()
        assert results[identity_migrations.CHAIN_NAME] == identity_migrations.warden_head()
        assert {"spark", "warden"} <= _schemas(owner)
        migrate.check_at_head(owner)


# -- Q32: a separate purge connection -------------------------------------------


async def _record_some(runtime: IdentityRuntime, count: int) -> None:
    action = next(iter(spark_audit.ACTIONS))
    for i in range(count):
        await runtime.recorder.record(
            AuditEvent(action=action, outcome="success", actor=f"test-{i}")
        )


@pytest.mark.postgres
class TestPurgeConnection:
    @pytest.fixture
    def purge_spy(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Run the real purge with a retention of 0 days, recording who it ran as."""
        calls: list[dict[str, Any]] = []
        real = pruning.purge
        monkeypatch.setattr(warden_retention, "MIN_RETENTION_DAYS", 0)

        async def spy(recorder: Any, keys: Any, *, retention_days: int, actor: Any = None) -> Any:
            async with recorder.db.connection() as conn:
                cur = await conn.execute("SELECT current_user")
                row = await cur.fetchone()
            call: dict[str, Any] = {"user": row[0], "recorder": recorder}
            calls.append(call)
            call["result"] = await real(recorder, keys, retention_days=0, actor=actor)
            return call["result"]

        monkeypatch.setattr(pruning, "purge", spy)
        return calls

    def test_the_workers_role_holds_no_delete_on_the_audit_trail(self, roles: Roles) -> None:
        for privilege in ("DELETE", "UPDATE"):
            assert (
                _privilege(
                    roles.owner,
                    "SELECT has_table_privilege(%s, 'warden.audit_events', %s)",
                    roles.worker_role,
                    privilege,
                )
                is False
            )
        assert (
            _privilege(
                roles.owner,
                "SELECT has_table_privilege(%s, 'warden.audit_events', 'DELETE')",
                roles.purger_role,
            )
            is True
        )

    async def test_with_the_purge_url_the_purge_runs_as_the_purger_role(
        self, roles: Roles, purge_spy: list[dict[str, Any]]
    ) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, roles.worker))
        app = App(_konfig(warden__audit__retention_days=400))
        pruning.register(app.settings, purge_database=roles.purger)
        [job] = [j for j in worker.registered_periodic() if j.name == pruning.AUDIT_PURGE]
        database = await Database.open(roles.worker)
        try:
            runtime = await IdentityRuntime.start(database, app.settings, app.secrets)
            try:
                await _record_some(runtime, 3)
                ctx = worker.WorkerContext(
                    "worker-purge", database, SyncDatabase(database), app, identity=runtime
                )
                await job.run(ctx)
            finally:
                runtime.close()
        finally:
            await database.close()
        [call] = purge_spy
        assert call["user"] == roles.purger_role
        assert call["result"].purged_rows >= 3
        # Only the checkpoint (and anything after it) is left.
        assert (
            _privilege(roles.owner, "SELECT min(id) FROM warden.audit_events")
            == call["result"].checkpoint_id
        )

    async def test_without_the_purge_url_the_purge_uses_the_workers_own_connection(
        self, roles: Roles, purge_spy: list[dict[str, Any]]
    ) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, roles.worker))
        app = App(_konfig(warden__audit__retention_days=400))
        pruning.register(app.settings)
        [job] = [j for j in worker.registered_periodic() if j.name == pruning.AUDIT_PURGE]
        database = await Database.open(roles.worker)
        try:
            runtime = await IdentityRuntime.start(database, app.settings, app.secrets)
            try:
                await _record_some(runtime, 2)
                ctx = worker.WorkerContext(
                    "worker-purge", database, SyncDatabase(database), app, identity=runtime
                )
                # The worker's own role may not delete audit rows: refused, as before.
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await job.run(ctx)
            finally:
                runtime.close()
        finally:
            await database.close()
        [call] = purge_spy
        assert call["user"] == roles.worker_role
        assert call["recorder"] is not None

    async def test_without_the_purge_url_the_owner_still_purges(
        self, roles: Roles, purge_spy: list[dict[str, Any]]
    ) -> None:
        mode.configure(RuntimeSettings(Mode.SERVER, roles.owner))
        app = App(_konfig(warden__audit__retention_days=400))
        pruning.register(app.settings)
        [job] = [j for j in worker.registered_periodic() if j.name == pruning.AUDIT_PURGE]
        database = await Database.open(roles.owner)
        try:
            runtime = await IdentityRuntime.start(database, app.settings, app.secrets)
            try:
                await _record_some(runtime, 2)
                ctx = worker.WorkerContext(
                    "worker-purge", database, SyncDatabase(database), app, identity=runtime
                )
                await job.run(ctx)
                [call] = purge_spy
                assert call["recorder"] is runtime.recorder
            finally:
                runtime.close()
        finally:
            await database.close()
        assert purge_spy[0]["result"].purged_rows >= 2


# -- the purge URL setting (no database) ----------------------------------------


class Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class TestThePurgeUrlSetting:
    def test_unset_or_blank_means_the_workers_own_connection(self) -> None:
        assert worker.audit_purge_database(Settings({}), MemorySecrets()) is None
        assert worker.audit_purge_database(Settings({PURGE_KEY: ""}), MemorySecrets()) is None
        assert worker.audit_purge_database(Settings({PURGE_KEY: "  "}), MemorySecrets()) is None

    def test_a_postgresql_url_is_used_as_given(self) -> None:
        url = "postgresql://purger:pw-dev-only@db.example.com:5432/spark"
        found = worker.audit_purge_database(Settings({PURGE_KEY: url}), MemorySecrets())
        assert found == DatabaseSettings(Dialect.POSTGRESQL, url=url)

    def test_the_sqlalchemy_scheme_is_normalised(self) -> None:
        url = "postgresql+psycopg://purger:pw-dev-only@db.example.com/spark"
        found = worker.audit_purge_database(Settings({PURGE_KEY: url}), MemorySecrets())
        assert (
            found is not None
            and found.url == "postgresql://purger:pw-dev-only@db.example.com/spark"
        )

    def test_a_secret_reference_is_resolved_through_the_store(self) -> None:
        url = "postgresql://purger:pw-dev-only@db.example.com/spark"
        secrets = MemorySecrets({"audit_purge_database_url": url})
        found = worker.audit_purge_database(
            Settings({PURGE_KEY: "secret://audit_purge_database_url"}), secrets
        )
        assert found == DatabaseSettings(Dialect.POSTGRESQL, url=url)

    def test_a_missing_secret_is_refused_by_name(self) -> None:
        with pytest.raises(worker.WorkerRefused, match="audit_purge_database_url") as caught:
            worker.audit_purge_database(
                Settings({PURGE_KEY: "secret://missing_purge_url"}), MemorySecrets()
            )
        assert "missing_purge_url" in str(caught.value)

    @pytest.mark.parametrize(
        "value",
        ["mysql://purger:hunter2-dev-only@db.example.com/spark", "sqlite:///spark.db", "nonsense"],
    )
    def test_a_url_that_is_not_postgresql_is_refused_without_echoing_it(self, value: str) -> None:
        with pytest.raises(worker.WorkerRefused, match=PURGE_KEY) as caught:
            worker.audit_purge_database(Settings({PURGE_KEY: value}), MemorySecrets())
        assert "hunter2" not in str(caught.value)
        assert value not in str(caught.value)

    def test_a_value_that_is_not_text_is_refused(self) -> None:
        with pytest.raises(worker.WorkerRefused, match=PURGE_KEY):
            worker.audit_purge_database(Settings({PURGE_KEY: 5}), MemorySecrets())

    def test_spark_worker_refuses_to_start_with_a_bad_purge_url(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        started: list[Any] = []

        class FakeWorker:
            def __init__(self, settings: Any, **kwargs: Any) -> None:
                started.append(kwargs)

            async def run(self, stop: asyncio.Event) -> None:
                return None

        bad = "mysql://purger:hunter2-dev-only@db.example.com/spark"

        def with_app(body: Any) -> tuple[Any, Any]:
            settings = _konfig(warden__audit__retention_days=400)
            settings.values["worker"] = {"audit_purge_database_url": bad}
            return None, asyncio.run(body(App(settings)))

        runtime = RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y")
        )
        monkeypatch.setattr(worker, "_load_runtime", lambda: runtime)
        monkeypatch.setattr(worker, "_check_schema", lambda settings: None)
        monkeypatch.setattr(worker, "Worker", FakeWorker)
        monkeypatch.setattr(worker, "_with_app", with_app)
        assert worker.main() == 2
        err = capsys.readouterr().err
        assert PURGE_KEY in err and "hunter2" not in err
        assert started == []

    def test_spark_worker_hands_the_purge_settings_to_the_purge_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        purger = DatabaseSettings(
            Dialect.POSTGRESQL, url="postgresql://purger@db.example.com/spark"
        )

        class FakeWorker:
            def __init__(self, settings: Any, **kwargs: Any) -> None:
                captured.update(kwargs)

            async def run(self, stop: asyncio.Event) -> None:
                return None

        def with_app(body: Any) -> tuple[Any, Any]:
            settings = _konfig(warden__audit__retention_days=400)
            settings.values["worker"] = {"audit_purge_database_url": "secret://purge_url"}
            app = App(settings)
            app.secrets.set("purge_url", purger.url or "")
            return None, asyncio.run(body(app))

        monkeypatch.setattr(worker, "Worker", FakeWorker)
        monkeypatch.setattr(worker, "_with_app", with_app)
        runtime = RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y")
        )
        worker._serve(runtime)
        [job] = [j for j in captured["periodic"] if j.name == pruning.AUDIT_PURGE]
        assert job.run.database == purger
