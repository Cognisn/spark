"""The connections revision: upgrade and downgrade on SQLite and PostgreSQL.

Upgrade adds the five connection tables and the usage columns; downgrade drops
them, leaving the rest of the schema as it was, and a second upgrade restores them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "connections"
PREVIOUS = "kg_build_jobs"
TABLES = {
    "connections",
    "connection_models",
    "catalogue_overlays",
    "connection_metering",
    "connection_assignments",
}
USAGE_COLUMNS = {"connection_id", "cache_read_tokens", "cache_write_tokens"}
NOW = "2026-11-16T00:00:00+00:00"
CONNECTION = (
    "INSERT INTO connections (id, name, kind, settings, residency, created_at, updated_at) "
    f"VALUES ('{{id}}', 'Work', 'ollama', '{{{{}}}}', '{{{{}}}}', '{NOW}', '{NOW}')"
)
ASSIGNMENT = (
    "INSERT INTO connection_assignments (connection_id, role, user_guid, monthly_credits, "
    f"updated_at) VALUES ('{{id}}', {{role}}, {{user}}, {{credits}}, '{NOW}')"
)
METERING = f"INSERT INTO connection_metering (connection_id, updated_at) VALUES ('{{id}}', '{NOW}')"
MODEL = (
    "INSERT INTO connection_models (connection_id, model_id, tier, updated_at) "
    f"VALUES ('{{id}}', 'llama3.1:8b', '{{tier}}', '{NOW}')"
)


def test_revision_follows_kg_build_jobs() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_revision_is_the_head() -> None:
    assert migrate.head() == REVISION


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

    def _usage_columns(self, settings: DatabaseSettings) -> set[str]:
        conn = self._connect(settings)
        try:
            rows = conn.execute("PRAGMA table_info(usage_tracking)").fetchall()
        finally:
            conn.close()
        return {r[1] for r in rows}

    def _exec(self, settings: DatabaseSettings, sql: str) -> None:
        conn = self._connect(settings)
        try:
            conn.execute(sql)
        finally:
            conn.close()

    def test_upgrade_adds_tables_and_usage_columns(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        assert TABLES <= self._tables(settings)
        assert USAGE_COLUMNS <= self._usage_columns(settings)

    def test_metering_defaults(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        self._exec(settings, CONNECTION.format(id="work"))
        self._exec(settings, METERING.format(id="work"))
        conn = self._connect(settings)
        try:
            row = conn.execute(
                "SELECT metered, input_weight, output_weight, cache_read_weight, "
                "cache_write_weight FROM connection_metering"
            ).fetchone()
        finally:
            conn.close()
        assert row == (0, 1, 5, 0.1, 1.25)

    def test_tier_and_assignment_target_are_checked(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        self._exec(settings, CONNECTION.format(id="work"))
        self._exec(settings, MODEL.format(id="work", tier="higher"))
        with pytest.raises(sqlite3.IntegrityError):
            self._exec(settings, MODEL.format(id="work", tier="premium"))
        self._exec(settings, ASSIGNMENT.format(id="work", role="'staff'", user="NULL", credits=10))
        with pytest.raises(sqlite3.IntegrityError):  # one row per role and connection
            self._exec(
                settings, ASSIGNMENT.format(id="work", role="'staff'", user="NULL", credits=5)
            )
        with pytest.raises(sqlite3.IntegrityError):  # a role or a user, never both
            self._exec(settings, ASSIGNMENT.format(id="work", role="'x'", user="'u1'", credits=1))
        with pytest.raises(sqlite3.IntegrityError):  # and never neither
            self._exec(settings, ASSIGNMENT.format(id="work", role="NULL", user="NULL", credits=1))

    def test_downgrade_removes_everything_it_added(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        migrate.downgrade(settings, target=PREVIOUS)
        assert not TABLES & self._tables(settings)
        assert not USAGE_COLUMNS & self._usage_columns(settings)
        assert "kg_build_jobs" in self._tables(settings)
        migrate.upgrade(settings)
        assert TABLES <= self._tables(settings)


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

    def _usage_columns(self, url: str) -> set[str]:
        rows = self._query(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'spark' AND table_name = 'usage_tracking'",
        )
        return {str(r[0]) for r in rows}

    def test_constraints_and_cascade(self, settings: DatabaseSettings) -> None:
        import psycopg

        migrate.upgrade(settings)
        url = settings.url
        assert url is not None
        self._exec(url, CONNECTION.format(id="work"))
        self._exec(url, MODEL.format(id="work", tier="lower"))
        self._exec(url, METERING.format(id="work"))
        self._exec(url, ASSIGNMENT.format(id="work", role="NULL", user="'u1'", credits="NULL"))
        with pytest.raises(psycopg.errors.CheckViolation):
            self._exec(url, MODEL.format(id="work", tier="premium"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            self._exec(url, ASSIGNMENT.format(id="work", role="NULL", user="'u1'", credits=3))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            self._exec(url, MODEL.format(id="missing", tier="standard"))
        self._exec(url, "DELETE FROM connections WHERE id = 'work'")
        for table in ("connection_models", "connection_metering", "connection_assignments"):
            assert self._query(url, f"SELECT COUNT(*) FROM spark.{table}") == [(0,)]

    def test_downgrade_removes_everything_it_added(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        url = settings.url
        assert url is not None
        assert TABLES <= self._tables(url)
        assert USAGE_COLUMNS <= self._usage_columns(url)
        migrate.downgrade(settings, target=PREVIOUS)
        assert not TABLES & self._tables(url)
        assert not USAGE_COLUMNS & self._usage_columns(url)
        migrate.upgrade(settings)
        assert TABLES <= self._tables(url)
