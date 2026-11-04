"""The knowledge graph build jobs revision: upgrade and downgrade on SQLite and PostgreSQL.

Upgrade adds ``kg_build_jobs`` with a partial unique index that allows one
active (queued or running) job per user and scope. Downgrade drops the table
and its indexes, leaving the rest of the schema as it was.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "kg_build_jobs"
PREVIOUS = "usage_budgets"
INSERT = (
    "INSERT INTO kg_build_jobs (user_guid, scope, model_id, status, created_at, updated_at) "
    "VALUES ('{user}', '{scope}', 'model-a', '{status}', '2026-11-03', '2026-11-03')"
)


def test_revision_follows_usage_budgets() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def _sqlite_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _sqlite_exec(path: Path, sql: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(sql)
    finally:
        conn.close()


class TestSqlite:
    @pytest.fixture
    def settings(self, tmp_path: Path) -> DatabaseSettings:
        return DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")

    def test_one_active_job_per_user_and_scope(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        path = settings.sqlite_path
        assert path is not None
        _sqlite_exec(path, INSERT.format(user="ada", scope="global", status="queued"))
        _sqlite_exec(path, INSERT.format(user="grace", scope="global", status="queued"))
        _sqlite_exec(path, INSERT.format(user="ada", scope="global", status="failed"))
        _sqlite_exec(path, INSERT.format(user="ada", scope="global", status="complete"))
        with pytest.raises(sqlite3.IntegrityError):
            _sqlite_exec(path, INSERT.format(user="ada", scope="global", status="running"))

    def test_status_is_checked(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        path = settings.sqlite_path
        assert path is not None
        with pytest.raises(sqlite3.IntegrityError):
            _sqlite_exec(path, INSERT.format(user="ada", scope="global", status="bogus"))

    def test_downgrade_removes_the_table(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        path = settings.sqlite_path
        assert path is not None
        assert "kg_build_jobs" in _sqlite_tables(path)
        migrate.downgrade(settings, target=PREVIOUS)
        assert "kg_build_jobs" not in _sqlite_tables(path)
        assert "kg_nodes" in _sqlite_tables(path)
        migrate.upgrade(settings)
        assert "kg_build_jobs" in _sqlite_tables(path)


@pytest.mark.postgres
class TestPostgres:
    @pytest.fixture
    def settings(self, pg_url: str) -> DatabaseSettings:
        return DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    @staticmethod
    def _tables(url: str) -> set[str]:
        import psycopg

        with psycopg.connect(url) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'spark'"
            ).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def _exec(url: str, sql: str) -> None:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SET search_path TO spark")
            conn.execute(sql)

    def test_one_active_job_per_user_and_scope(self, settings: DatabaseSettings) -> None:
        import psycopg

        migrate.upgrade(settings)
        url = settings.url
        assert url is not None
        self._exec(url, INSERT.format(user="ada", scope="global", status="queued"))
        self._exec(url, INSERT.format(user="grace", scope="global", status="running"))
        self._exec(url, INSERT.format(user="ada", scope="global", status="failed"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            self._exec(url, INSERT.format(user="ada", scope="global", status="running"))
        with pytest.raises(psycopg.errors.CheckViolation):
            self._exec(url, INSERT.format(user="ada", scope="conv:1", status="bogus"))

    def test_downgrade_removes_the_table(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        url = settings.url
        assert url is not None
        assert "kg_build_jobs" in self._tables(url)
        migrate.downgrade(settings, target=PREVIOUS)
        assert "kg_build_jobs" not in self._tables(url)
        assert "kg_nodes" in self._tables(url)
        migrate.upgrade(settings)
        assert "kg_build_jobs" in self._tables(url)
