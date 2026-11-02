"""The user identities revision: upgrade and downgrade on SQLite and PostgreSQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "user_identities"
PREVIOUS = "file_areas"
TABLE = "user_identities"


def _sqlite_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def _sqlite_columns(path: Path) -> dict[str, tuple[str, int, int]]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
    finally:
        conn.close()
    return {r[1]: (str(r[2]).upper(), int(r[3]), int(r[5])) for r in rows}


def _pg_tables(url: str) -> set[str]:
    import psycopg

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'spark'"
        ).fetchall()
    return {r[0] for r in rows}


def test_revision_follows_file_areas() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


class TestSqlite:
    def test_upgrade_adds_the_table_and_downgrade_removes_it(self, tmp_path: Path) -> None:
        path = tmp_path / "spark.db"
        settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
        migrate.upgrade(settings)
        assert TABLE in _sqlite_tables(path)
        columns = _sqlite_columns(path)
        assert columns["user_guid"] == ("TEXT", 0, 1)
        assert columns["warden_user_id"] == ("TEXT", 1, 0)
        assert "created_at" in columns
        migrate.downgrade(settings, target=PREVIOUS)
        assert TABLE not in _sqlite_tables(path)
        assert "user_files" in _sqlite_tables(path)
        assert migrate.current(settings) == PREVIOUS
        migrate.upgrade(settings)
        assert TABLE in _sqlite_tables(path)

    def test_warden_user_id_is_unique(self, tmp_path: Path) -> None:
        path = tmp_path / "spark.db"
        migrate.upgrade(DatabaseSettings(Dialect.SQLITE, sqlite_path=path))
        conn = sqlite3.connect(path)
        try:
            conn.execute(f"INSERT INTO {TABLE} (user_guid, warden_user_id) VALUES ('a', 'w')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"INSERT INTO {TABLE} (user_guid, warden_user_id) VALUES ('b', 'w')")
        finally:
            conn.close()


@pytest.mark.postgres
class TestPostgres:
    def test_upgrade_adds_the_table_and_downgrade_removes_it(self, pg_url: str) -> None:
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.upgrade(settings)
        assert TABLE in _pg_tables(pg_url)
        migrate.downgrade(settings, target=PREVIOUS)
        assert TABLE not in _pg_tables(pg_url)
        assert "user_files" in _pg_tables(pg_url)
        assert migrate.current(settings) == PREVIOUS
        migrate.upgrade(settings)
        assert TABLE in _pg_tables(pg_url)

    def test_warden_user_id_is_unique(self, pg_url: str) -> None:
        import psycopg

        migrate.upgrade(DatabaseSettings(Dialect.POSTGRESQL, url=pg_url))
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f"INSERT INTO spark.{TABLE} (user_guid, warden_user_id) VALUES ('a', 'w')")
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    f"INSERT INTO spark.{TABLE} (user_guid, warden_user_id) VALUES ('b', 'w')"
                )
