"""The per-user action names revision: upgrade and downgrade on SQLite and PostgreSQL.

Upgrade replaces the global ``UNIQUE(name)`` of ``autonomous_actions`` with
``UNIQUE(user_guid, name)``. Downgrade restores the global constraint, and
refuses (changing nothing) while two users share a name, rather than dropping
anyone's action.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "action_names_per_user"
PREVIOUS = "user_identities"
INSERT = (
    "INSERT INTO {table} (name, action_prompt, model_id, user_guid) VALUES ('{name}', 'p', 'm', "
    "'{user}')"
)


def test_revision_follows_user_identities() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS
    # Later revisions follow it, so it is on the chain to head rather than head itself.
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


@contextmanager
def _sqlite(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def _sqlite_insert(path: Path, name: str, user: str) -> None:
    with _sqlite(path) as conn:
        conn.execute(INSERT.format(table="autonomous_actions", name=name, user=user))


def _sqlite_rows(path: Path, sql: str) -> list[Any]:
    with _sqlite(path) as conn:
        return conn.execute(sql).fetchall()


class TestSqlite:
    @pytest.fixture
    def settings(self, tmp_path: Path) -> DatabaseSettings:
        return DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")

    def test_names_are_unique_per_user_at_head(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        path = settings.sqlite_path
        assert path is not None
        _sqlite_insert(path, "nightly", "ada")
        _sqlite_insert(path, "nightly", "grace")
        with pytest.raises(sqlite3.IntegrityError):
            _sqlite_insert(path, "nightly", "grace")

    def test_upgrade_keeps_rows_runs_and_indexes(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings, target=PREVIOUS)
        path = settings.sqlite_path
        assert path is not None
        _sqlite_insert(path, "nightly", "ada")
        with _sqlite(path) as conn:
            conn.execute(
                "INSERT INTO action_runs (action_id, status, user_guid) VALUES (1, 'completed', "
                "'ada')"
            )
        migrate.upgrade(settings)
        assert _sqlite_rows(
            path, "SELECT id, name, user_guid, is_enabled FROM autonomous_actions"
        ) == [(1, "nightly", "ada", 1)]
        assert _sqlite_rows(path, "SELECT action_id FROM action_runs") == [(1,)]
        indexes = {
            r[0]
            for r in _sqlite_rows(
                path, "SELECT name FROM sqlite_master WHERE tbl_name = 'autonomous_actions'"
            )
        }
        assert {"idx_actions_enabled", "idx_actions_next_run"} <= indexes
        # New rows continue after the existing ids.
        _sqlite_insert(path, "daily", "ada")
        assert _sqlite_rows(path, "SELECT MAX(id) FROM autonomous_actions") == [(2,)]

    def test_downgrade_restores_the_global_constraint(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings)
        path = settings.sqlite_path
        assert path is not None
        _sqlite_insert(path, "nightly", "ada")
        migrate.downgrade(settings, target=PREVIOUS)
        assert migrate.current(settings) == PREVIOUS
        assert _sqlite_rows(path, "SELECT name, user_guid FROM autonomous_actions") == [
            ("nightly", "ada")
        ]
        with pytest.raises(sqlite3.IntegrityError):
            _sqlite_insert(path, "nightly", "grace")
        migrate.upgrade(settings)
        _sqlite_insert(path, "nightly", "grace")

    def test_downgrade_refuses_a_shared_name(self, settings: DatabaseSettings) -> None:
        migrate.upgrade(settings, target=REVISION)
        path = settings.sqlite_path
        assert path is not None
        _sqlite_insert(path, "nightly", "ada")
        _sqlite_insert(path, "nightly", "grace")
        with pytest.raises(Exception, match="more than one user"):
            migrate.downgrade(settings, target=PREVIOUS)
        assert migrate.current(settings) == REVISION
        assert sorted(_sqlite_rows(path, "SELECT name, user_guid FROM autonomous_actions")) == [
            ("nightly", "ada"),
            ("nightly", "grace"),
        ]


def _pg_execute(url: str, sql: str) -> list[Any]:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(sql)
        return cur.fetchall() if cur.description else []


def _pg_insert(url: str, name: str, user: str) -> None:
    _pg_execute(url, INSERT.format(table="spark.autonomous_actions", name=name, user=user))


@pytest.mark.postgres
class TestPostgres:
    def test_names_are_unique_per_user_at_head(self, pg_url: str) -> None:
        import psycopg

        migrate.upgrade(DatabaseSettings(Dialect.POSTGRESQL, url=pg_url))
        _pg_insert(pg_url, "nightly", "ada")
        _pg_insert(pg_url, "nightly", "grace")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _pg_insert(pg_url, "nightly", "grace")

    def test_downgrade_restores_the_global_constraint(self, pg_url: str) -> None:
        import psycopg

        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.upgrade(settings)
        _pg_insert(pg_url, "nightly", "ada")
        migrate.downgrade(settings, target=PREVIOUS)
        assert migrate.current(settings) == PREVIOUS
        with pytest.raises(psycopg.errors.UniqueViolation):
            _pg_insert(pg_url, "nightly", "grace")
        migrate.upgrade(settings)
        _pg_insert(pg_url, "nightly", "grace")

    def test_downgrade_refuses_a_shared_name(self, pg_url: str) -> None:
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.upgrade(settings, target=REVISION)
        _pg_insert(pg_url, "nightly", "ada")
        _pg_insert(pg_url, "nightly", "grace")
        with pytest.raises(Exception, match="more than one user"):
            migrate.downgrade(settings, target=PREVIOUS)
        assert migrate.current(settings) == REVISION
        rows = _pg_execute(pg_url, "SELECT name, user_guid FROM spark.autonomous_actions")
        assert sorted(rows) == [("nightly", "ada"), ("nightly", "grace")]
