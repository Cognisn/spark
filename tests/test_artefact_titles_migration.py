"""The artefact_titles revision: on the chain, its back-fill, and its downgrade on both engines.

Existing files are named by their file name and take their kind from their source:
an upload or an imported file is an attachment, a document fetched for the preview is
a preview, and anything SPARK made is a document. The downgrade drops both columns and
keeps every file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "artefact_titles"
PREVIOUS = "conversation_sharing"

ROWS = [
    ("u", "uploads/1/budget.xlsx", "budget.xlsx", "upload", "attachment"),
    ("u", "previews/2/report.pdf", "report.pdf", "preview", "preview"),
    ("u", "notes/plan.docx", "plan.docx", "created", "document"),
    ("u", "imported/old.txt", "old.txt", "import", "attachment"),
]


def test_revision_follows_conversation_sharing() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_on_the_chain_to_head() -> None:
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


def _sqlite_columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(user_files)").fetchall()}


def test_upgrade_backfills_and_downgrade_drops_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "spark.db"
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
    migrate.upgrade(settings, target=PREVIOUS)
    with sqlite3.connect(path) as conn:
        for user, key, name, source, _ in ROWS:
            conn.execute(
                "INSERT INTO user_files (user_guid, storage_key, filename, size_bytes, source)"
                " VALUES (?, ?, ?, 1, ?)",
                (user, key, name, source),
            )
    assert not {"title", "kind"} & _sqlite_columns(path)
    migrate.upgrade(settings)
    with sqlite3.connect(path) as conn:
        got = conn.execute("SELECT storage_key, title, kind FROM user_files ORDER BY id").fetchall()
    assert got == [(key, name, kind) for _, key, name, _, kind in ROWS]
    migrate.downgrade(settings, target=PREVIOUS)
    assert not {"title", "kind"} & _sqlite_columns(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM user_files").fetchone() == (len(ROWS),)
    migrate.upgrade(settings)
    assert {"title", "kind"} <= _sqlite_columns(path)


@pytest.mark.postgres
def test_upgrade_backfills_and_downgrade_drops_on_postgresql(pg_url: str) -> None:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    def columns() -> set[str]:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'spark' AND table_name = 'user_files'"
            ).fetchall()
        return {r[0] for r in rows}

    migrate.upgrade(settings, target=PREVIOUS)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for user, key, name, source, _ in ROWS:
            conn.execute(
                "INSERT INTO spark.user_files (user_guid, storage_key, filename, size_bytes,"
                " source) VALUES (%s, %s, %s, 1, %s)",
                (user, key, name, source),
            )
    assert not {"title", "kind"} & columns()
    migrate.upgrade(settings)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        got = conn.execute(
            "SELECT storage_key, title, kind FROM spark.user_files ORDER BY id"
        ).fetchall()
    assert got == [(key, name, kind) for _, key, name, _, kind in ROWS]
    migrate.downgrade(settings, target=PREVIOUS)
    assert not {"title", "kind"} & columns()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM spark.user_files").fetchone() == (len(ROWS),)
