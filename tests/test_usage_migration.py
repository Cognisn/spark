"""The usage budgets revision: upgrade and downgrade on SQLite and PostgreSQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "usage_budgets"
PREVIOUS = "action_names_per_user"
NEW_TABLES = {"usage_role_budgets", "usage_user_budgets"}
NEW_COLUMNS = {"source", "action_id", "period"}


def _sqlite_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _sqlite_columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _pg_tables(url: str) -> set[str]:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'spark'"
        ).fetchall()
    return {r[0] for r in rows}


def _pg_columns(url: str, table: str) -> set[str]:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'spark' AND table_name = %s",
            (table,),
        ).fetchall()
    return {r[0] for r in rows}


def test_revision_follows_action_names_per_user() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


class TestSqlite:
    def test_upgrade_adds_budgets_and_columns_and_downgrade_removes_them(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "spark.db"
        settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
        migrate.upgrade(settings)
        assert NEW_TABLES <= _sqlite_tables(path)
        assert NEW_COLUMNS <= _sqlite_columns(path, "usage_tracking")

        migrate.downgrade(settings, target=PREVIOUS)
        assert not NEW_TABLES & _sqlite_tables(path)
        assert not NEW_COLUMNS & _sqlite_columns(path, "usage_tracking")
        assert "usage_tracking" in _sqlite_tables(path)
        assert migrate.current(settings) == PREVIOUS

        migrate.upgrade(settings)
        assert NEW_TABLES <= _sqlite_tables(path)

    def test_existing_rows_get_their_period_from_their_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "spark.db"
        settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
        migrate.upgrade(settings, target=PREVIOUS)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT INTO usage_tracking (model_id, input_tokens, output_tokens, timestamp,"
                " user_guid) VALUES ('m', 1, 2, '2026-10-31T23:59:59+00:00', 'u1')"
            )
            conn.commit()
        finally:
            conn.close()
        migrate.upgrade(settings)
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("SELECT period, source FROM usage_tracking").fetchone()
        finally:
            conn.close()
        assert row == ("2026-10", "chat")


@pytest.mark.postgres
class TestPostgres:
    def test_upgrade_adds_budgets_and_columns_and_downgrade_removes_them(self, pg_url: str) -> None:
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.upgrade(settings)
        assert NEW_TABLES <= _pg_tables(pg_url)
        assert NEW_COLUMNS <= _pg_columns(pg_url, "usage_tracking")

        migrate.downgrade(settings, target=PREVIOUS)
        assert not NEW_TABLES & _pg_tables(pg_url)
        assert not NEW_COLUMNS & _pg_columns(pg_url, "usage_tracking")
        assert migrate.current(settings) == PREVIOUS

        migrate.upgrade(settings)
        assert NEW_TABLES <= _pg_tables(pg_url)

    def test_existing_rows_get_their_period_from_their_timestamp(self, pg_url: str) -> None:
        import psycopg

        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.upgrade(settings, target=PREVIOUS)
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO spark.usage_tracking (model_id, input_tokens, output_tokens,"
                " timestamp, user_guid) VALUES ('m', 1, 2, '2026-10-31 23:59:59', 'u1')"
            )
        migrate.upgrade(settings)
        with psycopg.connect(pg_url) as conn:
            row = conn.execute("SELECT period, source FROM spark.usage_tracking").fetchone()
        assert row == ("2026-10", "chat")
