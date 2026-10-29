"""The file areas revision: upgrade and downgrade on SQLite and PostgreSQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "file_areas"
TABLES = {"user_files", "user_file_usage"}


def _sqlite_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _pg_tables(url: str) -> set[str]:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'spark'"
        ).fetchall()
    return {r[0] for r in rows}


def test_revision_follows_the_baseline() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == "0001"


class TestSqlite:
    def test_upgrade_adds_the_tables_and_downgrade_removes_them(self, tmp_path: Path) -> None:
        path = tmp_path / "spark.db"
        settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
        migrate.upgrade(settings)
        assert TABLES <= _sqlite_tables(path)
        migrate.downgrade(settings, target="0001")
        assert not TABLES & _sqlite_tables(path)
        assert "conversation_files" in _sqlite_tables(path)
        migrate.upgrade(settings)
        assert TABLES <= _sqlite_tables(path)


@pytest.mark.postgres
class TestPostgres:
    def test_upgrade_adds_the_tables_and_downgrade_removes_them(self, pg_url: str) -> None:
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.upgrade(settings)
        assert TABLES <= _pg_tables(pg_url)
        migrate.downgrade(settings, target="0001")
        assert not TABLES & _pg_tables(pg_url)
        assert "conversation_files" in _pg_tables(pg_url)
        migrate.upgrade(settings)
        assert TABLES <= _pg_tables(pg_url)
