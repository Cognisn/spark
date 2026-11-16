"""The credit metering revision: upgrade and downgrade on SQLite and PostgreSQL.

Upgrade adds the credit columns to ``usage_tracking`` and drops A14's token budget
tables; downgrade restores the tables (empty) and drops the columns, and a second
upgrade applies again.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "credit_metering"
PREVIOUS = "connections"
BUDGET_TABLES = {"usage_role_budgets", "usage_user_budgets"}
CREDIT_COLUMNS = {"credits_micro", "credit_inputs"}
OLD_ROW = (
    "INSERT INTO usage_tracking (model_id, input_tokens, output_tokens, timestamp, user_guid, "
    "period) VALUES ('m', 1, 2, '2026-11-01 00:00:00', 'u1', '2026-11')"
)


def test_revision_follows_connections() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_on_the_chain_to_head() -> None:
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


class TestSqlite:
    @pytest.fixture
    def settings(self, tmp_path: Path) -> DatabaseSettings:
        return DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")

    @staticmethod
    def _connect(settings: DatabaseSettings) -> sqlite3.Connection:
        assert settings.sqlite_path is not None
        return sqlite3.connect(settings.sqlite_path, isolation_level=None)

    def _tables(self, settings: DatabaseSettings) -> set[str]:
        conn = self._connect(settings)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows}

    def _columns(self, settings: DatabaseSettings) -> set[str]:
        conn = self._connect(settings)
        try:
            rows = conn.execute("PRAGMA table_info(usage_tracking)").fetchall()
        finally:
            conn.close()
        return {r[1] for r in rows}

    def test_upgrade_adds_credits_and_drops_token_budgets(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings, target=PREVIOUS)
        assert BUDGET_TABLES <= self._tables(settings)
        conn = self._connect(settings)
        try:
            conn.execute(OLD_ROW)
        finally:
            conn.close()
        migrate.upgrade(settings, target=REVISION)
        assert CREDIT_COLUMNS <= self._columns(settings)
        assert not BUDGET_TABLES & self._tables(settings)
        conn = self._connect(settings)
        try:
            row = conn.execute("SELECT credits_micro, credit_inputs FROM usage_tracking").fetchone()
        finally:
            conn.close()
        assert row == (0, None)  # a row from before metering cost nothing

    def test_downgrade_restores_the_budget_tables(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings, target=REVISION)
        migrate.downgrade(settings, target=PREVIOUS)
        assert BUDGET_TABLES <= self._tables(settings)
        assert not CREDIT_COLUMNS & self._columns(settings)
        assert migrate.current(settings) == PREVIOUS
        conn = self._connect(settings)
        try:
            conn.execute("INSERT INTO usage_role_budgets (role, monthly_tokens) VALUES ('u', 5)")
            conn.execute("INSERT INTO usage_user_budgets (user_guid) VALUES ('u1')")
        finally:
            conn.close()
        migrate.upgrade(settings)
        assert CREDIT_COLUMNS <= self._columns(settings)
        assert not BUDGET_TABLES & self._tables(settings)


@pytest.mark.postgres
class TestPostgres:
    @pytest.fixture
    def settings(self, pg_url: str) -> DatabaseSettings:
        return DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    @staticmethod
    def _query(url: str, sql: str) -> list[tuple[object, ...]]:
        import psycopg

        with psycopg.connect(url) as conn:
            return list(conn.execute(sql).fetchall())

    @staticmethod
    def _exec(url: str, sql: str) -> None:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SET search_path TO spark")
            conn.execute(sql)

    def _tables(self, url: str) -> set[str]:
        rows = self._query(
            url, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'spark'"
        )
        return {str(r[0]) for r in rows}

    def _columns(self, url: str) -> set[str]:
        rows = self._query(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'spark' AND table_name = 'usage_tracking'",
        )
        return {str(r[0]) for r in rows}

    def test_upgrade_downgrade_upgrade(self, settings: DatabaseSettings) -> None:
        url = settings.url
        assert url is not None
        migrate.upgrade(settings, target=PREVIOUS)
        self._exec(url, OLD_ROW)
        migrate.upgrade(settings, target=REVISION)
        assert CREDIT_COLUMNS <= self._columns(url)
        assert not BUDGET_TABLES & self._tables(url)
        assert self._query(
            url, "SELECT credits_micro, credit_inputs FROM spark.usage_tracking"
        ) == [(0, None)]
        migrate.downgrade(settings, target=PREVIOUS)
        assert BUDGET_TABLES <= self._tables(url)
        assert not CREDIT_COLUMNS & self._columns(url)
        self._exec(url, "INSERT INTO usage_role_budgets (role, monthly_tokens) VALUES ('u', 5)")
        migrate.upgrade(settings)
        assert CREDIT_COLUMNS <= self._columns(url)
        assert not BUDGET_TABLES & self._tables(url)
