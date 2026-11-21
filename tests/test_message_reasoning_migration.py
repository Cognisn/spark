"""The message_reasoning revision: on the chain, and its upgrade and downgrade on both engines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "message_reasoning"
PREVIOUS = "bootstrap_record"


def test_revision_follows_bootstrap_record() -> None:
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
    assert "message_reasoning" not in _tables(path)
    migrate.upgrade(settings)
    assert "message_reasoning" in _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO conversations (name, user_guid) VALUES ('c', 'u')")
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, user_guid)"
            " VALUES (1, 'assistant', 'answer', 'u')"
        )
        conn.execute("INSERT INTO message_reasoning VALUES (1, 1, 'thought')")
        with pytest.raises(sqlite3.IntegrityError):  # one row per message
            conn.execute("INSERT INTO message_reasoning VALUES (1, 1, 'again')")
        with pytest.raises(sqlite3.IntegrityError):  # the message must exist
            conn.execute("INSERT INTO message_reasoning VALUES (9, 1, 'orphan')")
        conn.execute("DELETE FROM messages WHERE id = 1")  # goes with its message
        assert conn.execute("SELECT count(*) FROM message_reasoning").fetchone() == (0,)
    migrate.downgrade(settings, target=PREVIOUS)
    assert "message_reasoning" not in _tables(path)


@pytest.mark.postgres
def test_upgrade_and_downgrade_on_postgresql(pg_url: str) -> None:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    def exists() -> bool:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT to_regclass('spark.message_reasoning') IS NOT NULL"
            ).fetchone()
        return bool(row and row[0])

    migrate.upgrade(settings, target=PREVIOUS)
    assert not exists()
    migrate.upgrade(settings)
    assert exists()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("INSERT INTO spark.conversations (name, user_guid) VALUES ('c', 'u')")
        cid = conn.execute("SELECT id FROM spark.conversations").fetchone()[0]  # type: ignore[index]
        mid = conn.execute(
            "INSERT INTO spark.messages (conversation_id, role, content, user_guid)"
            " VALUES (%s, 'assistant', 'answer', 'u') RETURNING id",
            (cid,),
        ).fetchone()[
            0
        ]  # type: ignore[index]
        conn.execute("INSERT INTO spark.message_reasoning VALUES (%s, %s, 'thought')", (mid, cid))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO spark.message_reasoning VALUES (%s, %s, 'again')", (mid, cid))
        conn.execute("DELETE FROM spark.messages WHERE id = %s", (mid,))
        row = conn.execute("SELECT count(*) FROM spark.message_reasoning").fetchone()
        assert row == (0,)
    migrate.downgrade(settings, target=PREVIOUS)
    assert not exists()
