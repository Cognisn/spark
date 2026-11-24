"""The conversation_sharing revision: on the chain, the owner back-fill, the turn table and the
per-participant tool permission key, with its upgrade and downgrade on both engines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "conversation_sharing"
PREVIOUS = "message_reasoning"


def test_revision_follows_message_reasoning() -> None:
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
        conn.execute(
            "INSERT INTO conversations (name, user_guid, is_favourite) VALUES ('a', 'ada', 1)"
        )
        conn.execute("INSERT INTO conversations (name, user_guid) VALUES ('b', 'bob')")
        conn.execute(
            "INSERT INTO conversation_tool_permissions"
            " (conversation_id, tool_name, permission_state, user_guid)"
            " VALUES (1, 'web_search', 'allowed', 'ada')"
        )
    assert "conversation_members" not in _tables(path)

    migrate.upgrade(settings, target=REVISION)
    assert {"conversation_members", "conversation_turns"} <= _tables(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        rows = conn.execute(
            "SELECT conversation_id, user_guid, role, added_by, last_seen_at IS NOT NULL,"
            " is_favourite FROM conversation_members ORDER BY conversation_id"
        ).fetchall()
        # Each owner back-filled, already seen, keeping their star.
        assert rows == [(1, "ada", "owner", "ada", 1, 1), (2, "bob", "owner", "bob", 1, 0)]
        with pytest.raises(sqlite3.IntegrityError):  # one owner per conversation
            conn.execute(
                "INSERT INTO conversation_members (conversation_id, user_guid, role, added_by)"
                " VALUES (1, 'eve', 'owner', 'ada')"
            )
        with pytest.raises(sqlite3.IntegrityError):  # a known role only
            conn.execute(
                "INSERT INTO conversation_members (conversation_id, user_guid, role, added_by)"
                " VALUES (1, 'eve', 'admin', 'ada')"
            )
        conn.execute(
            "INSERT INTO conversation_members (conversation_id, user_guid, role, added_by)"
            " VALUES (1, 'eve', 'contributor', 'ada')"
        )
        # The tool permission key is per participant now: a second user's row fits.
        conn.execute(
            "INSERT INTO conversation_tool_permissions"
            " (conversation_id, tool_name, permission_state, user_guid)"
            " VALUES (1, 'web_search', 'denied', 'eve')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO conversation_tool_permissions"
                " (conversation_id, tool_name, permission_state, user_guid)"
                " VALUES (1, 'web_search', 'denied', 'eve')"
            )
        conn.execute(
            "INSERT INTO conversation_turns VALUES (1, 'ada', 's1', 'r1', '2026-01-01', '2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):  # one turn per conversation
            conn.execute(
                "INSERT INTO conversation_turns"
                " VALUES (1, 'eve', 's2', 'r1', '2026-01-01', '2026-01-01')"
            )
        conn.execute("DELETE FROM conversation_turns")

    # Two participants hold a permission for one tool: the downgrade refuses, changing nothing.
    with pytest.raises(Exception, match="more than one participant"):
        migrate.downgrade(settings, target=PREVIOUS)
    assert "conversation_members" in _tables(path)

    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM conversation_tool_permissions WHERE user_guid = 'eve'")
    migrate.downgrade(settings, target=PREVIOUS)
    assert "conversation_members" not in _tables(path)
    assert "conversation_turns" not in _tables(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT conversation_id, tool_name, user_guid FROM conversation_tool_permissions"
        ).fetchall() == [(1, "web_search", "ada")]
        with pytest.raises(sqlite3.IntegrityError):  # the narrow key is back
            conn.execute(
                "INSERT INTO conversation_tool_permissions"
                " (conversation_id, tool_name, permission_state, user_guid)"
                " VALUES (1, 'web_search', 'denied', 'eve')"
            )


@pytest.mark.postgres
def test_upgrade_and_downgrade_on_postgresql(pg_url: str) -> None:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    def exists(table: str) -> bool:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"spark.{table}",)).fetchone()
        return bool(row and row[0])

    migrate.upgrade(settings, target=PREVIOUS)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        ada = conn.execute(
            "INSERT INTO spark.conversations (name, user_guid, is_favourite)"
            " VALUES ('a', 'ada', 1) RETURNING id"
        ).fetchone()[
            0
        ]  # type: ignore[index]
        conn.execute(
            "INSERT INTO spark.conversation_tool_permissions"
            " (conversation_id, tool_name, permission_state, user_guid)"
            " VALUES (%s, 'web_search', 'allowed', 'ada')",
            (ada,),
        )
    assert not exists("conversation_members")

    migrate.upgrade(settings, target=REVISION)
    assert exists("conversation_members") and exists("conversation_turns")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT user_guid, role, last_seen_at IS NOT NULL, is_favourite"
            " FROM spark.conversation_members WHERE conversation_id = %s",
            (ada,),
        ).fetchall()
        assert rows == [("ada", "owner", True, 1)]
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO spark.conversation_members"
                " (conversation_id, user_guid, role, added_by) VALUES (%s, 'eve', 'owner', 'ada')",
                (ada,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO spark.conversation_members"
                " (conversation_id, user_guid, role, added_by) VALUES (%s, 'eve', 'admin', 'ada')",
                (ada,),
            )
        conn.execute(
            "INSERT INTO spark.conversation_tool_permissions"
            " (conversation_id, tool_name, permission_state, user_guid)"
            " VALUES (%s, 'web_search', 'denied', 'eve')",
            (ada,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO spark.conversation_tool_permissions"
                " (conversation_id, tool_name, permission_state, user_guid)"
                " VALUES (%s, 'web_search', 'denied', 'eve')",
                (ada,),
            )

    with pytest.raises(Exception, match="more than one participant"):
        migrate.downgrade(settings, target=PREVIOUS)
    assert exists("conversation_members")

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("DELETE FROM spark.conversation_tool_permissions WHERE user_guid = 'eve'")
    migrate.downgrade(settings, target=PREVIOUS)
    assert not exists("conversation_members") and not exists("conversation_turns")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO spark.conversation_tool_permissions"
                " (conversation_id, tool_name, permission_state, user_guid)"
                " VALUES (%s, 'web_search', 'denied', 'eve')",
                (ada,),
            )
