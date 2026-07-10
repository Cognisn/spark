"""Orchestrator tests with scripted stub services (no real LLM)."""

from __future__ import annotations

import pytest

from spark.core.debate.orchestrator import DebateOrchestrator
from spark.database import Database, debates
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    """Create a fresh test database and return its connection."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


class ScriptedService:
    """Stub LLM service returning queued responses."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self._model = None

    def set_model(self, model_id: str) -> None:
        self._model = model_id

    def invoke_model(self, messages, **kwargs):
        return self.responses.pop(0)


def text_response(text: str) -> dict:
    return {
        "content": text,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_use": None,
        "content_blocks": [{"type": "text", "text": text}],
    }


def tool_response(name: str, tool_input: dict, text: str = "") -> dict:
    return {
        "content": text,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_use": [{"id": "t1", "name": name, "input": tool_input}],
        "content_blocks": [],
    }


AGENTS = {
    "pro": {"model_id": "model-pro", "brief": None},
    "con": {"model_id": "model-con", "brief": None},
    "judge": {"model_id": "model-judge", "brief": None},
}


def _setup_debate(db, rounds_mode="fixed", max_rounds=1) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        f"VALUES ({ph}, {ph}, {ph}, 'debate')",
        ("D", "model-judge", "u1"),
    )
    db.commit()
    cid = cur.lastrowid
    debates.create_debate(db, cid, "Topic T", rounds_mode, max_rounds, "u1", AGENTS)
    return cid


def make_orchestrator(db, services: dict, events: list) -> DebateOrchestrator:
    return DebateOrchestrator(
        db,
        lambda model_id: services[model_id],
        {},  # embedded tools config: empty, debaters get only submit_argument
        status_callback=lambda t, d: events.append((t, d)),
    )


class TestFullDebate:
    def test_one_round_fixed_debate_reaches_qa(self, db) -> None:
        cid = _setup_debate(db)
        services = {
            "model-judge": ScriptedService(
                [
                    tool_response(
                        "set_speaking_order",
                        {"first_speaker": "pro"},
                        "Welcome to the debate.",
                    ),
                    text_response("Pro argued better. Pro wins."),  # ruling (forced at cap)
                ]
            ),
            "model-pro": ScriptedService(
                [
                    tool_response(
                        "submit_argument",
                        {
                            "argument_markdown": "Pro case.",
                            "exhibits": [{"title": "E", "content": "data"}],
                        },
                    ),
                ]
            ),
            "model-con": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "Con case."})]
            ),
        }
        events: list = []
        orch = make_orchestrator(db, services, events)
        orch.run(cid, "u1")

        d = debates.get_debate(db, cid)
        assert d["state"] == "qa"
        assert d["opening_speaker"] == "pro"
        types = [t["turn_type"] for t in debates.get_turns(db, cid)]
        assert types == ["announcement", "argument", "argument", "ruling"]
        roles = [t["role"] for t in debates.get_turns(db, cid) if t["turn_type"] == "argument"]
        assert roles == ["pro", "con"]
        ex = debates.get_exhibits(db, cid)
        assert any(items[0]["label"] == "A" for items in ex.values())
        event_types = [t for t, _ in events]
        assert "debate_state" in event_types and "exhibits" in event_types

    def test_failed_turn_pauses_and_is_retryable(self, db) -> None:
        cid = _setup_debate(db)

        class ExplodingService(ScriptedService):
            def invoke_model(self, messages, **kwargs):
                raise RuntimeError("provider down")

        services = {
            "model-judge": ScriptedService(
                [
                    tool_response("set_speaking_order", {"first_speaker": "pro"}, "Open."),
                    text_response("Ruling."),
                ]
            ),
            "model-pro": ExplodingService([]),
            "model-con": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "Con case."})]
            ),
        }
        events: list = []
        orch = make_orchestrator(db, services, events)
        orch.run(cid, "u1")  # returns on failure, state still 'round'

        d = debates.get_debate(db, cid)
        assert d["state"] == "round"
        turns = debates.get_turns(db, cid)
        assert turns[-1]["turn_type"] == "argument" and turns[-1]["status"] == "failed"

        # Retry: replace the failing service and run again; the failed turn is redone.
        services["model-pro"] = ScriptedService(
            [tool_response("submit_argument", {"argument_markdown": "Pro case."})]
        )
        orch2 = make_orchestrator(db, services, events)
        orch2.run(cid, "u1")
        assert debates.get_debate(db, cid)["state"] == "qa"

    def test_user_prompt_and_qa(self, db) -> None:
        cid = _setup_debate(db)
        services = {
            "model-judge": ScriptedService(
                [
                    tool_response("set_speaking_order", {"first_speaker": "pro"}, "Open."),
                    text_response("Ruling."),
                    text_response("Because the exhibits were stronger."),  # qa answer
                ]
            ),
            "model-pro": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "P."})]
            ),
            "model-con": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "C."})]
            ),
        }
        orch = make_orchestrator(db, services, [])
        orch.run(cid, "u1")
        qid = orch.submit_user_prompt(cid, "Why did pro win?")
        answer = orch.answer_qa(cid, "u1", qid)
        assert "exhibits were stronger" in answer
        types = [t["turn_type"] for t in debates.get_turns(db, cid)]
        assert types[-2:] == ["qa_question", "qa_answer"]


class TestFloorEvents:
    def test_floor_sequence_one_round(self, db) -> None:
        cid = _setup_debate(db)
        services = {
            "model-judge": ScriptedService(
                [
                    tool_response("set_speaking_order", {"first_speaker": "pro"}, "Open."),
                    text_response("Ruling."),
                ]
            ),
            "model-pro": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "P."})]
            ),
            "model-con": ScriptedService(
                [tool_response("submit_argument", {"argument_markdown": "C."})]
            ),
        }
        events: list = []
        orch = make_orchestrator(db, services, events)
        orch.run(cid, "u1")
        floor = [d["role"] for t, d in events if t == "floor"]
        # judge opens, pro argues, con argues, judge rules; none between each
        assert floor == ["judge", "none", "pro", "none", "con", "none", "judge", "none"]


class TestCapabilityEnforcement:
    def _agents(self, **overrides):
        agents = {
            "pro": {"model_id": "model-pro", "brief": None},
            "con": {"model_id": "model-con", "brief": None},
            "judge": {"model_id": "model-judge", "brief": None},
        }
        for role, extra in overrides.items():
            agents[role].update(extra)
        return agents

    def _run(self, db, agents):
        ph = db.placeholder
        cur = db.execute(
            f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
            f"VALUES ({ph}, {ph}, {ph}, 'debate')",
            ("D", "model-judge", "u1"),
        )
        db.commit()
        cid = cur.lastrowid
        debates.create_debate(db, cid, "Topic T", "fixed", 1, "u1", agents)

        captured = {"pro": [], "con": [], "judge": []}

        class Recording(ScriptedService):
            def __init__(self, key, responses):
                super().__init__(responses)
                self._key = key

            def invoke_model(self, messages, **kwargs):
                captured[self._key].append(kwargs)
                return super().invoke_model(messages, **kwargs)

        services = {
            "model-judge": Recording(
                "judge",
                [
                    tool_response("set_speaking_order", {"first_speaker": "pro"}, "Open."),
                    text_response("Ruling."),
                ],
            ),
            "model-pro": Recording(
                "pro", [tool_response("submit_argument", {"argument_markdown": "P."})]
            ),
            "model-con": Recording(
                "con", [tool_response("submit_argument", {"argument_markdown": "C."})]
            ),
        }
        orch = DebateOrchestrator(
            db,
            lambda m: services[m],
            {"embedded_tools": {}},
            status_callback=lambda t, d: None,
        )
        orch.run(cid, "u1")
        return captured

    def test_tool_allowlist_restricts_offer(self, db) -> None:
        captured = self._run(db, self._agents(pro={"allowed_tools": []}))
        pro_tools = {t["name"] for c in captured["pro"] for t in (c.get("tools") or [])}
        con_tools = {t["name"] for c in captured["con"] for t in (c.get("tools") or [])}
        assert "submit_argument" in pro_tools  # always survives
        assert "get_current_datetime" not in pro_tools
        assert "get_current_datetime" in con_tools  # unrestricted keeps builtins

    def test_skill_allowlist_filters_block_and_tools(self, db, tmp_path) -> None:
        from spark.skills.manager import SkillsManager, set_skills_manager
        from tests.test_skills_manager import make_skill

        make_skill(tmp_path / "user", "pdf-filler", description="Fill PDF forms.")
        make_skill(tmp_path / "user", "weekly-report", description="Weekly report.")
        set_skills_manager(SkillsManager(tmp_path / "user"))
        try:
            captured = self._run(
                db,
                self._agents(
                    pro={"allowed_skills": ["pdf-filler"]},
                    con={"allowed_skills": []},
                ),
            )
            pro_system = captured["pro"][0].get("system") or ""
            assert "pdf-filler" in pro_system and "weekly-report" not in pro_system
            con_system = captured["con"][0].get("system") or ""
            assert "## Available Skills" not in con_system
            con_tools = {t["name"] for c in captured["con"] for t in (c.get("tools") or [])}
            assert "use_skill" not in con_tools
        finally:
            set_skills_manager(None)

    def test_judge_skill_allowlist(self, db, tmp_path) -> None:
        from spark.skills.manager import SkillsManager, set_skills_manager
        from tests.test_skills_manager import make_skill

        make_skill(tmp_path / "user", "pdf-filler", description="Fill PDF forms.")
        set_skills_manager(SkillsManager(tmp_path / "user"))
        try:
            captured = self._run(db, self._agents(judge={"allowed_skills": []}))
            judge_tools = {
                t["name"] for c in captured["judge"] for t in (c.get("tools") or [])
            }
            assert "use_skill" not in judge_tools
            assert "set_speaking_order" in judge_tools  # procedural untouched
        finally:
            set_skills_manager(None)
