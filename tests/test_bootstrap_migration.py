"""The bootstrap_record revision: on the chain, and its upgrade and downgrade on both engines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "bootstrap_record"
PREVIOUS = "overlay_connection_scope"


def test_revision_follows_overlay_connection_scope() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_on_the_chain_to_head() -> None:
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


def _sqlite_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "spark.db"
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
    migrate.upgrade(settings, target=PREVIOUS)
    assert "bootstrap" not in _sqlite_tables(path)
    migrate.upgrade(settings)
    assert "bootstrap" in _sqlite_tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO bootstrap (id, completed_at, spark_version) VALUES (1, '2026-11-19', 'x')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bootstrap (id, completed_at, spark_version) "
                "VALUES (2, '2026-11-19', 'x')"
            )
    migrate.downgrade(settings, target=PREVIOUS)
    assert "bootstrap" not in _sqlite_tables(path)


@pytest.mark.postgres
def test_upgrade_and_downgrade_on_postgresql(pg_url: str) -> None:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    def exists() -> bool:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            row = conn.execute("SELECT to_regclass('spark.bootstrap') IS NOT NULL").fetchone()
        return bool(row and row[0])

    migrate.upgrade(settings, target=PREVIOUS)
    assert not exists()
    migrate.upgrade(settings)
    assert exists()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO spark.bootstrap (id, completed_at, spark_version) "
                "VALUES (2, now(), 'x')"
            )
    migrate.downgrade(settings, target=PREVIOUS)
    assert not exists()
