"""warden's migration chain in SPARK: registration, ``spark migrate``, head checks and the shared pool.

warden is PostgreSQL only and runs its own Alembic chain in the ``warden``
schema, after SPARK's. Personal mode (SQLite) skips it and is otherwise
unchanged. A server-mode database whose warden chain is behind is refused like
SPARK's own (:class:`MigrationRequired`).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spark import worker
from spark.core import application
from spark.db import migrate
from spark.db.migrate import DatabaseTooNew, MigrationRequired
from spark.identity import migrations as identity_migrations
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

WARDEN_HEAD = "7b2e9d4c1f08"


@pytest.fixture(autouse=True)
def _isolated_chains() -> Iterator[None]:
    saved = migrate.registered_chains()
    migrate.clear_chains()
    yield
    migrate.clear_chains()
    for chain in saved:
        migrate.register_chain(chain)


def _sqlite(path: Path) -> DatabaseSettings:
    return DatabaseSettings(Dialect.SQLITE, sqlite_path=path)


def _pg(url: str) -> DatabaseSettings:
    return DatabaseSettings(Dialect.POSTGRESQL, url=url)


def _sqlite_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def _pg_tables(url: str, schema: str) -> set[str]:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
        ).fetchall()
    return {r[0] for r in rows}


def _warden_revision(url: str) -> str | None:
    import warden.db

    return warden.db.current_revision(url)


class TestRegistration:
    def test_register_adds_the_warden_chain_once(self) -> None:
        identity_migrations.register()
        identity_migrations.register()
        chains = migrate.registered_chains()
        assert [c.name for c in chains] == ["warden"]
        assert chains[0] is identity_migrations.WARDEN_CHAIN

    def test_warden_head_is_the_one_warden_ships(self) -> None:
        assert identity_migrations.warden_head() == WARDEN_HEAD

    def test_run_migrations_registers_the_chain(self, tmp_path: Path) -> None:
        runtime = RuntimeSettings(Mode.PERSONAL, _sqlite(tmp_path / "spark.db"))
        application.run_migrations(runtime)
        assert "warden" in [c.name for c in migrate.registered_chains()]

    def test_the_worker_schema_check_registers_the_chain(self, tmp_path: Path) -> None:
        settings = _sqlite(tmp_path / "spark.db")
        migrate.upgrade(settings)
        worker._check_schema(settings)
        assert "warden" in [c.name for c in migrate.registered_chains()]

    async def test_the_web_lifespan_registers_the_chain(self, tmp_path: Path) -> None:
        import spark.web.server as server

        app = _lifespan_app(_sqlite(tmp_path / "spark.db"), auto_migrate=True)
        async with server._lifespan(app):  # type: ignore[arg-type]
            assert "warden" in [c.name for c in migrate.registered_chains()]


def _lifespan_app(settings: DatabaseSettings, *, auto_migrate: bool) -> Any:
    state = SimpleNamespace(
        database_settings=settings,
        database_auto_migrate=auto_migrate,
        database_ready=threading.Event(),
    )
    return SimpleNamespace(state=state)


class TestPersonalModeIsUnchanged:
    def test_the_chain_is_skipped_on_sqlite(self, tmp_path: Path) -> None:
        path = tmp_path / "spark.db"
        assert identity_migrations.upgrade(_sqlite(path)) is None

    def test_migrate_all_leaves_no_warden_tables(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain.db"
        migrate.upgrade(_sqlite(plain))
        path = tmp_path / "spark.db"
        identity_migrations.register()
        assert migrate.migrate_all(_sqlite(path)) == {"spark": migrate.head(), "warden": None}
        assert _sqlite_tables(path) == _sqlite_tables(plain)

    def test_prepare_and_check_pass_on_sqlite(self, tmp_path: Path) -> None:
        identity_migrations.register()
        path = tmp_path / "spark.db"
        migrate.prepare(_sqlite(path), auto_migrate=True)
        migrate.check_at_head(_sqlite(path))
        identity_migrations.check_at_head(_sqlite(path))

    def test_spark_migrate_prints_only_spark_in_personal_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime = RuntimeSettings(Mode.PERSONAL, _sqlite(tmp_path / "spark.db"))
        _fake_cli(monkeypatch, tmp_path, runtime)
        assert application.run_migrate() == 0
        assert capsys.readouterr().out.splitlines() == [f"spark: {migrate.head()}"]


class _FakeContext:
    def __init__(self, **_: Any) -> None:
        self.settings = SimpleNamespace(reload=lambda: None)
        self.secrets = SimpleNamespace()

    async def __aenter__(self) -> _FakeContext:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _fake_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime: RuntimeSettings) -> None:
    """``spark migrate`` with its configuration replaced by ``runtime``."""
    monkeypatch.setattr(application, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(application, "AppContext", _FakeContext)
    monkeypatch.setattr(application, "configure_runtime", lambda settings, secrets=None: runtime)


@pytest.mark.postgres
class TestServerMode:
    def test_migrate_all_runs_spark_then_warden(self, pg_url: str) -> None:
        identity_migrations.register()
        results = migrate.migrate_all(_pg(pg_url))
        assert results == {"spark": migrate.head(), "warden": WARDEN_HEAD}
        assert list(results) == ["spark", "warden"]
        assert "users" in _pg_tables(pg_url, "warden")
        assert "user_identities" in _pg_tables(pg_url, "spark")
        assert _warden_revision(pg_url) == WARDEN_HEAD

    def test_migrating_twice_is_a_no_op(self, pg_url: str) -> None:
        identity_migrations.register()
        first = migrate.migrate_all(_pg(pg_url))
        tables = _pg_tables(pg_url, "warden"), _pg_tables(pg_url, "spark")
        assert migrate.migrate_all(_pg(pg_url)) == first
        assert (_pg_tables(pg_url, "warden"), _pg_tables(pg_url, "spark")) == tables

    def test_run_migrations_includes_warden(self, pg_url: str) -> None:
        runtime = RuntimeSettings(Mode.SERVER, _pg(pg_url))
        assert application.run_migrations(runtime) == {
            "spark": migrate.head(),
            "warden": WARDEN_HEAD,
        }

    def test_spark_migrate_prints_both_chains(
        self,
        pg_url: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _fake_cli(monkeypatch, tmp_path, RuntimeSettings(Mode.SERVER, _pg(pg_url)))
        assert application.run_migrate() == 0
        assert capsys.readouterr().out.splitlines() == [
            f"spark: {migrate.head()}",
            f"warden: {WARDEN_HEAD}",
        ]

    def test_a_database_with_warden_behind_is_refused(self, pg_url: str) -> None:
        settings = _pg(pg_url)
        migrate.upgrade(settings)
        migrate.check_at_head(settings)  # nothing registered: SPARK's chain alone
        identity_migrations.register()
        with pytest.raises(MigrationRequired, match="spark migrate"):
            migrate.check_at_head(settings)
        with pytest.raises(MigrationRequired):
            migrate.prepare(settings, auto_migrate=False)
        migrate.migrate_all(settings)
        migrate.check_at_head(settings)
        migrate.prepare(settings, auto_migrate=False)

    def test_a_warden_chain_part_way_is_refused(self, pg_url: str) -> None:
        import warden.db

        settings = _pg(pg_url)
        migrate.upgrade(settings)
        warden.db.migrate(pg_url, revision="5c2f9a1e7b04")
        identity_migrations.register()
        with pytest.raises(MigrationRequired, match="warden"):
            migrate.check_at_head(settings)

    def test_a_warden_chain_newer_than_this_code_is_refused(self, pg_url: str) -> None:
        import psycopg

        settings = _pg(pg_url)
        identity_migrations.register()
        migrate.migrate_all(settings)
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("UPDATE warden.alembic_version SET version_num = 'ffffffffffff'")
        with pytest.raises(DatabaseTooNew, match="warden"):
            migrate.check_at_head(settings)

    def test_personal_style_auto_migration_brings_warden_to_head(self, pg_url: str) -> None:
        identity_migrations.register()
        migrate.prepare(_pg(pg_url), auto_migrate=True)
        assert _warden_revision(pg_url) == WARDEN_HEAD

    def test_the_worker_refuses_a_database_with_warden_behind(self, pg_url: str) -> None:
        settings = _pg(pg_url)
        migrate.upgrade(settings)
        with pytest.raises(MigrationRequired):
            worker._check_schema(settings)

    async def test_the_web_lifespan_refuses_a_database_with_warden_behind(
        self, pg_url: str
    ) -> None:
        import spark.web.server as server

        settings = _pg(pg_url)
        await _to_thread(migrate.upgrade, settings)
        app = _lifespan_app(settings, auto_migrate=False)
        with pytest.raises(MigrationRequired):
            async with server._lifespan(app):  # type: ignore[arg-type]
                pytest.fail("the lifespan must not open the database")
        assert isinstance(app.state.database_error, MigrationRequired)


async def _to_thread(fn: Any, *args: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args)
