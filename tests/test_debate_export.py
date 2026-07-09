"""Tests for the debate exporter."""

from __future__ import annotations

import json

import pytest

from spark.core.debate.export import export_debate_json, export_debate_markdown
from spark.database import Database, debates
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


AGENTS = {
    "pro": {"model_id": "model-a", "brief": None},
    "con": {"model_id": "model-b", "brief": None},
    "judge": {"model_id": "model-c", "brief": None},
}


def _setup_debate(db) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        f"VALUES ({ph}, {ph}, {ph}, 'debate')",
        ("D", "model-c", "u1"),
    )
    db.commit()
    cid = cur.lastrowid
    debates.create_debate(db, cid, "Topic T", "fixed", 1, "u1", AGENTS)
    return cid


def test_markdown_export_structure(db) -> None:
    cid = _setup_debate(db)
    tid = debates.add_turn(db, cid, 1, "pro", "argument", "Pro case.")
    debates.add_exhibits(
        db, tid, [{"label": "A", "title": "T", "content": "C", "source": "s"}]
    )
    debates.add_turn(db, cid, 0, "judge", "ruling", "Pro wins.")

    md = export_debate_markdown(db, cid)
    assert "# Debate: Topic T" in md
    assert "## Round 1" in md and "Pro case." in md
    assert "Exhibit A" in md and "## Ruling" in md and "Pro wins." in md


def test_failed_turns_excluded(db) -> None:
    cid = _setup_debate(db)
    debates.add_turn(db, cid, 1, "pro", "argument", "FAILED DRAFT", status="failed")
    md = export_debate_markdown(db, cid)
    assert "FAILED DRAFT" not in md


def test_json_export_round_trips(db) -> None:
    cid = _setup_debate(db)
    debates.add_turn(db, cid, 1, "pro", "argument", "Pro case.")
    data = json.loads(export_debate_json(db, cid))
    assert data["topic"] == "Topic T"
    assert data["turns"][0]["content"] == "Pro case."
