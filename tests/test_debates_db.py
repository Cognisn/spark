"""Tests for the debate database schema and operations."""

from __future__ import annotations

import pytest


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


from spark.database import debates

AGENTS = {
    "pro": {"model_id": "model-a", "brief": "argue for"},
    "con": {"model_id": "model-b", "brief": "argue against"},
    "judge": {"model_id": "model-c", "brief": None},
}


async def _conv(db) -> int:
    return await db.fetch_value(
        "INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        "VALUES (?, ?, ?, ?) RETURNING id",
        ("Debate", "model-c", "u1", "debate"),
    )


class TestDebateSchema:
    async def test_debate_tables_exist(self, db) -> None:
        for table in (
            "debate_config",
            "debate_agents",
            "debate_turns",
            "debate_exhibits",
        ):
            assert await db.fetch_value(f"SELECT COUNT(*) FROM {table}") == 0

    async def test_conversation_type_column(self, db) -> None:
        await db.execute(
            "INSERT INTO conversations (name, model_id, user_guid) VALUES (?, ?, ?)",
            ("t", "m", "u"),
        )
        assert await db.fetch_value("SELECT conversation_type FROM conversations") == "standard"


class TestDebatesCrud:
    async def test_create_and_get(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "Is tea better than coffee", "fixed", 2, "u1", AGENTS)
        d = await debates.get_debate(db, cid)
        assert d["topic"] == "Is tea better than coffee"
        assert d["state"] == "setup"
        assert d["agents"]["pro"]["model_id"] == "model-a"
        assert d["agents"]["judge"]["brief"] is None

    async def test_get_missing_returns_none(self, db) -> None:
        assert await debates.get_debate(db, 9999) is None

    async def test_state_update(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "T", "judge", None, "u1", AGENTS)
        await debates.update_debate_state(db, cid, "round", current_round=1, opening_speaker="con")
        d = await debates.get_debate(db, cid)
        assert (d["state"], d["current_round"], d["opening_speaker"]) == ("round", 1, "con")

    async def test_turns_and_exhibits(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        tid = await debates.add_turn(db, cid, 1, "pro", "argument", "My case", token_count=42)
        await debates.add_exhibits(
            db,
            tid,
            [{"label": "A", "title": "Study", "content": "Data", "source": "https://x"}],
        )
        turns = await debates.get_turns(db, cid)
        assert turns[0]["turn_type"] == "argument" and turns[0]["round"] == 1
        ex = await debates.get_exhibits(db, cid)
        assert ex[tid][0]["label"] == "A"

    async def test_update_turn_and_tokens(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        tid = await debates.add_turn(db, cid, 1, "pro", "argument", "x", status="failed")
        await debates.update_turn(db, tid, status="complete", summary="short")
        await debates.add_agent_tokens(db, cid, "pro", 100, 50)
        turns = await debates.get_turns(db, cid)
        assert turns[0]["status"] == "complete" and turns[0]["summary"] == "short"
        d = await debates.get_debate(db, cid)
        assert d["agents"]["pro"]["tokens_sent"] == 100


class TestCapabilityColumns:
    async def test_allowlists_round_trip(self, db) -> None:
        cid = await _conv(db)
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
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", agents)
        d = await debates.get_debate(db, cid)
        assert d["agents"]["pro"]["allowed_tools"] == ["web_search"]
        assert d["agents"]["pro"]["allowed_skills"] == []
        assert d["agents"]["con"]["allowed_tools"] is None
        assert d["agents"]["judge"]["allowed_skills"] == ["skill-creator"]

    async def test_corrupt_json_treated_as_null(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        await db.execute(
            "UPDATE debate_agents SET allowed_tools = 'not-json' "
            "WHERE conversation_id = ? AND role = 'pro'",
            (cid,),
        )
        assert (await debates.get_debate(db, cid))["agents"]["pro"]["allowed_tools"] is None


class TestPanelAgentColumns:
    async def test_display_name_and_human_round_trip(self, db) -> None:
        cid = await _conv(db)
        agents = {
            "moderator": {"model_id": "m", "brief": None, "display_name": "Moderator"},
            "panellist:1": {
                "model_id": "a",
                "brief": "economist",
                "display_name": "Economist",
            },
            "panellist:2": {
                "model_id": "",
                "brief": None,
                "display_name": "Matthew",
                "is_human": True,
            },
        }
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", agents)
        d = await debates.get_debate(db, cid)
        assert d["agents"]["panellist:1"]["display_name"] == "Economist"
        assert d["agents"]["panellist:2"]["is_human"] is True
        assert d["agents"]["moderator"]["is_human"] is False

    async def test_debate_agents_unaffected(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        d = await debates.get_debate(db, cid)
        assert d["agents"]["pro"]["display_name"] is None
        assert d["agents"]["pro"]["is_human"] is False


class TestVoiceIdColumn:
    async def test_voice_id_round_trips(self, db) -> None:
        cid = await _conv(db)
        agents = {
            "moderator": {
                "model_id": "m",
                "brief": None,
                "display_name": "Mod",
                "voice_id": "voice-mod",
            },
            "panellist:1": {
                "model_id": "a",
                "brief": None,
                "display_name": "Econ",
                "voice_id": "voice-econ",
            },
            "panellist:2": {
                "model_id": "",
                "brief": None,
                "display_name": "Matthew",
                "is_human": True,
            },
        }
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", agents)
        d = await debates.get_debate(db, cid)

        assert d["agents"]["moderator"]["voice_id"] == "voice-mod"
        assert d["agents"]["panellist:1"]["voice_id"] == "voice-econ"
        # The human panellist speaks for themselves, so they have no voice.
        assert d["agents"]["panellist:2"]["voice_id"] is None

    async def test_absent_voice_id_is_none(self, db) -> None:
        cid = await _conv(db)
        await debates.create_debate(db, cid, "T", "fixed", 1, "u1", AGENTS)
        d = await debates.get_debate(db, cid)
        assert d["agents"]["pro"]["voice_id"] is None
