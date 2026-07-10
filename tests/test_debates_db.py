"""Tests for the debate database schema and operations."""

from __future__ import annotations

import pytest

from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    """Create a fresh test database and return its connection."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


from spark.database import debates

AGENTS = {
    "pro": {"model_id": "model-a", "brief": "argue for"},
    "con": {"model_id": "model-b", "brief": "argue against"},
    "judge": {"model_id": "model-c", "brief": None},
}


def _conv(db) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        f"VALUES ({ph}, {ph}, {ph}, {ph})",
        ("Debate", "model-c", "u1", "debate"),
    )
    db.commit()
    return cur.lastrowid


class TestDebateSchema:
    def test_debate_tables_exist(self, db) -> None:
        for table in (
            "debate_config",
            "debate_agents",
            "debate_turns",
            "debate_exhibits",
        ):
            cur = db.execute(f"SELECT COUNT(*) FROM {table}")
            assert cur.fetchone()[0] == 0

    def test_conversation_type_column(self, db) -> None:
        ph = db.placeholder
        db.execute(
            f"INSERT INTO conversations (name, model_id, user_guid) VALUES ({ph}, {ph}, {ph})",
            ("t", "m", "u"),
        )
        db.commit()
        cur = db.execute("SELECT conversation_type FROM conversations")
        assert cur.fetchone()[0] == "standard"

    def test_schema_initialise_idempotent(self, db) -> None:
        from spark.database.schema import initialise_schema

        initialise_schema(db)  # second run must not raise
        initialise_schema(db)  # third run must not raise


class TestDebatesCrud:
    def test_create_and_get(self, db) -> None:
        cid = _conv(db)
        debates.create_debate(db, cid, "Is tea better than coffee", "fixed", 2, "u1", AGENTS)
        d = debates.get_debate(db, cid)
        assert d["topic"] == "Is tea better than coffee"
        assert d["state"] == "setup"
        assert d["agents"]["pro"]["model_id"] == "model-a"
        assert d["agents"]["judge"]["brief"] is None

    def test_get_missing_returns_none(self, db) -> None:
        assert debates.get_debate(db, 9999) is None

    def test_state_update(self, db) -> None:
        cid = _conv(db)
        debates.create_debate(db, cid, "T", "judge", None, "u1", AGENTS)
        debates.update_debate_state(db, cid, "round", current_round=1, opening_speaker="con")
        d = debates.get_debate(db, cid)
        assert (d["state"], d["current_round"], d["opening_speaker"]) == ("round", 1, "con")

    def test_turns_and_exhibits(self, db) -> None:
        cid = _conv(db)
        debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        tid = debates.add_turn(db, cid, 1, "pro", "argument", "My case", token_count=42)
        debates.add_exhibits(
            db,
            tid,
            [{"label": "A", "title": "Study", "content": "Data", "source": "https://x"}],
        )
        turns = debates.get_turns(db, cid)
        assert turns[0]["turn_type"] == "argument" and turns[0]["round"] == 1
        ex = debates.get_exhibits(db, cid)
        assert ex[tid][0]["label"] == "A"

    def test_update_turn_and_tokens(self, db) -> None:
        cid = _conv(db)
        debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        tid = debates.add_turn(db, cid, 1, "pro", "argument", "x", status="failed")
        debates.update_turn(db, tid, status="complete", summary="short")
        debates.add_agent_tokens(db, cid, "pro", 100, 50)
        turns = debates.get_turns(db, cid)
        assert turns[0]["status"] == "complete" and turns[0]["summary"] == "short"
        d = debates.get_debate(db, cid)
        assert d["agents"]["pro"]["tokens_sent"] == 100


class TestCapabilityColumns:
    def test_allowlists_round_trip(self, db) -> None:
        cid = _conv(db)
        agents = {
            "pro": {
                "model_id": "a",
                "brief": None,
                "allowed_tools": ["web_search"],
                "allowed_skills": [],
            },
            "con": {"model_id": "b", "brief": None},
            "judge": {"model_id": "c", "brief": None, "allowed_skills": ["skill-creator"]},
        }
        debates.create_debate(db, cid, "T", "fixed", 1, "u1", agents)
        d = debates.get_debate(db, cid)
        assert d["agents"]["pro"]["allowed_tools"] == ["web_search"]
        assert d["agents"]["pro"]["allowed_skills"] == []
        assert d["agents"]["con"]["allowed_tools"] is None
        assert d["agents"]["judge"]["allowed_skills"] == ["skill-creator"]

    def test_corrupt_json_treated_as_null(self, db) -> None:
        cid = _conv(db)
        debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        ph = db.placeholder
        db.execute(
            f"UPDATE debate_agents SET allowed_tools = 'not-json' "
            f"WHERE conversation_id = {ph} AND role = 'pro'",
            (cid,),
        )
        db.commit()
        assert debates.get_debate(db, cid)["agents"]["pro"]["allowed_tools"] is None
