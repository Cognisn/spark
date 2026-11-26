"""The project_cards revision: on the chain after ``projects``, the one suggestion per
conversation at the database boundary, cards leaving with their conversation, and its
upgrade and downgrade on both engines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "project_cards"
PREVIOUS = "projects"

_CARD = (
    "INSERT INTO project_cards (id, user_guid, conversation_id, kind, payload, created_at,"
    " expires_at) VALUES (?, 'ada', ?, ?, '{}', '2026-11-26T00:00:00.000000Z',"
    " '2026-11-27T00:00:00.000000Z')"
)


def test_revision_follows_projects() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_on_the_chain_to_head() -> None:
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "spark.db"
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
    migrate.upgrade(settings, target=PREVIOUS)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO conversations (name, user_guid) VALUES ('a', 'ada')")
        conn.execute("INSERT INTO conversations (name, user_guid) VALUES ('b', 'ada')")
    assert "project_cards" not in _tables(path)

    migrate.upgrade(settings, target=REVISION)
    assert "project_cards" in _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(_CARD, ("s1", 1, "suggestion"))
        conn.execute(_CARD, ("u1", 1, "update"))
        conn.execute(_CARD, ("u2", 1, "update"))
        conn.execute(_CARD, ("s2", 2, "suggestion"))
        # One suggestion per conversation, whatever its state.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_CARD, ("s3", 1, "suggestion"))
        for bad in (("x1", 1, "delete"),):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(_CARD, bad)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE project_cards SET status = 'shared' WHERE id = 'u1'")
        # A deleted conversation takes its cards.
        conn.execute("DELETE FROM conversations WHERE id = 1")
        left = conn.execute("SELECT id FROM project_cards ORDER BY id").fetchall()
        assert left == [("s2",)]

    migrate.downgrade(settings, target=PREVIOUS)
    assert "project_cards" not in _tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM conversations").fetchall() == [("b",)]


@pytest.mark.postgres
def test_upgrade_and_downgrade_on_postgresql(pg_url: str) -> None:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    def exists() -> bool:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            row = conn.execute("SELECT to_regclass('spark.project_cards') IS NOT NULL").fetchone()
        return bool(row and row[0])

    migrate.upgrade(settings, target=PREVIOUS)
    assert not exists()
    migrate.upgrade(settings, target=REVISION)
    assert exists()
    card = _CARD.replace("project_cards", "spark.project_cards").replace("?", "%s")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        cid = conn.execute(
            "INSERT INTO spark.conversations (name, user_guid) VALUES ('a', 'ada') RETURNING id"
        ).fetchone()[  # type: ignore[index]
            0
        ]
        conn.execute(card, ("s1", cid, "suggestion"))
        conn.execute(card, ("u1", cid, "update"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(card, ("s2", cid, "suggestion"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(card, ("x1", cid, "delete"))
        conn.execute("DELETE FROM spark.conversations WHERE id = %s", (cid,))
        assert conn.execute("SELECT COUNT(*) FROM spark.project_cards").fetchone() == (0,)

    migrate.downgrade(settings, target=PREVIOUS)
    assert not exists()
