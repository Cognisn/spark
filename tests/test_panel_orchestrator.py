"""Panel orchestrator: rotation, human pause and resume, allowlists."""

from __future__ import annotations

import json

import pytest

from spark.core.panel.orchestrator import PanelOrchestrator
from spark.database import Database, debates
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from tests.test_debate_orchestrator import ScriptedService, text_response, tool_response


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    """Create a fresh test database and return its connection."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


def _panel(db, *, human=False, rounds_mode="fixed", max_rounds=1) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        f"VALUES ({ph}, {ph}, {ph}, 'panel')",
        ("P", "model-mod", "u1"),
    )
    db.commit()
    cid = cur.lastrowid
    agents = {
        "moderator": {"model_id": "model-mod", "brief": None, "display_name": "Moderator"},
        "panellist:1": {
            "model_id": "model-p1",
            "brief": "economist",
            "display_name": "Economist",
        },
        "panellist:2": {
            "model_id": "model-p2",
            "brief": "engineer",
            "display_name": "Engineer",
        },
    }
    if human:
        agents["panellist:3"] = {
            "model_id": "",
            "brief": None,
            "display_name": "Matthew",
            "is_human": True,
        }
    debates.create_debate(db, cid, "Topic T", rounds_mode, max_rounds, "u1", agents)
    return cid


def _services(order: list[str]) -> dict[str, ScriptedService]:
    return {
        "model-mod": ScriptedService(
            [
                tool_response("set_speaking_order", {"order": order}, "Welcome."),
                text_response("Synthesis: broad agreement."),
            ]
        ),
        "model-p1": ScriptedService(
            [tool_response("submit_contribution", {"contribution_markdown": "Econ view."})]
        ),
        "model-p2": ScriptedService(
            [tool_response("submit_contribution", {"contribution_markdown": "Eng view."})]
        ),
    }


def make(db, services: dict, events: list | None = None) -> PanelOrchestrator:
    return PanelOrchestrator(
        db,
        lambda m: services[m],
        {},
        status_callback=(lambda t, d: events.append((t, d))) if events is not None else None,
    )


class TestRotation:
    def test_one_round_follows_order_and_synthesises(self, db) -> None:
        cid = _panel(db)
        events: list = []
        orch = make(db, _services(["panellist:2", "panellist:1"]), events)
        orch.run(cid, "u1")

        d = debates.get_debate(db, cid)
        assert d["state"] == "qa"
        assert json.loads(d["opening_speaker"]) == ["panellist:2", "panellist:1"]
        contribs = [
            t for t in debates.get_turns(db, cid) if t["turn_type"] == "contribution"
        ]
        assert [c["role"] for c in contribs] == ["panellist:2", "panellist:1"]
        types = [t["turn_type"] for t in debates.get_turns(db, cid)]
        assert "synthesis" in types
        names = [d2.get("name") for t2, d2 in events if t2 == "panellist_turn_start"]
        assert names == ["Engineer", "Economist"]

    def test_invalid_order_falls_back_to_numeric(self, db) -> None:
        cid = _panel(db)
        orch = make(db, _services(["ghost"]))
        orch.run(cid, "u1")
        d = debates.get_debate(db, cid)
        assert json.loads(d["opening_speaker"]) == ["panellist:1", "panellist:2"]


class TestHumanTurn:
    def test_pause_at_human_then_resume(self, db) -> None:
        cid = _panel(db, human=True)
        events: list = []
        services = _services(["panellist:1", "panellist:3", "panellist:2"])
        orch = make(db, services, events)
        orch.run(cid, "u1")

        # Paused at Matthew: state still round, human_turn emitted, p2 not yet run
        d = debates.get_debate(db, cid)
        assert d["state"] == "round"
        assert any(t == "human_turn" and dd["name"] == "Matthew" for t, dd in events)
        assert len(services["model-p2"].responses) == 1  # untouched
        pending = orch.awaiting_human(cid, "u1")
        assert pending is not None
        assert pending["role"] == "panellist:3"
        assert pending["round"] == 1

        # Contribution arrives; resume completes the round and synthesises
        turn_id = orch.submit_contribution(cid, "u1", "My human take.")
        assert turn_id is not None
        orch.run(cid, "u1")
        assert debates.get_debate(db, cid)["state"] == "qa"
        contribs = [
            t["role"]
            for t in debates.get_turns(db, cid)
            if t["turn_type"] == "contribution"
        ]
        assert contribs == ["panellist:1", "panellist:3", "panellist:2"]

    def test_submit_contribution_refused_when_not_pending(self, db) -> None:
        cid = _panel(db)  # no human panellist
        orch = make(db, _services(["panellist:1", "panellist:2"]))
        assert orch.submit_contribution(cid, "u1", "x") is None


class TestUserPromptAndQa:
    def test_qa_question_answered_by_moderator(self, db) -> None:
        cid = _panel(db)
        services = _services(["panellist:1", "panellist:2"])
        orch = make(db, services)
        orch.run(cid, "u1")
        assert debates.get_debate(db, cid)["state"] == "qa"

        services["model-mod"].responses.append(text_response("Because they agreed."))
        qid = orch.submit_user_prompt(cid, "Why consensus?")
        answer = orch.answer_qa(cid, "u1", qid)
        assert answer == "Because they agreed."
        types = [t["turn_type"] for t in debates.get_turns(db, cid)]
        assert "qa_question" in types
        assert "qa_answer" in types


class TestCapabilities:
    def test_allowlist_enforced_per_panellist(self, db) -> None:
        ph = db.placeholder
        cur = db.execute(
            f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
            f"VALUES ({ph}, {ph}, {ph}, 'panel')",
            ("P", "model-mod", "u1"),
        )
        db.commit()
        cid = cur.lastrowid
        agents = {
            "moderator": {
                "model_id": "model-mod",
                "brief": None,
                "display_name": "Moderator",
            },
            "panellist:1": {
                "model_id": "model-p1",
                "brief": None,
                "display_name": "Restricted",
                "allowed_tools": [],
            },
            "panellist:2": {
                "model_id": "model-p2",
                "brief": None,
                "display_name": "Free",
            },
        }
        debates.create_debate(db, cid, "T", "fixed", 1, "u1", agents)

        captured: dict[str, list] = {"model-p1": [], "model-p2": []}

        class Recording(ScriptedService):
            def __init__(self, key, responses):
                super().__init__(responses)
                self._key = key

            def invoke_model(self, messages, **kwargs):
                if self._key in captured:
                    captured[self._key].append(kwargs)
                return super().invoke_model(messages, **kwargs)

        services = {
            "model-mod": Recording(
                "model-mod",
                [
                    tool_response(
                        "set_speaking_order",
                        {"order": ["panellist:1", "panellist:2"]},
                        "Open.",
                    ),
                    text_response("Synthesis."),
                ],
            ),
            "model-p1": Recording(
                "model-p1",
                [tool_response("submit_contribution", {"contribution_markdown": "A."})],
            ),
            "model-p2": Recording(
                "model-p2",
                [tool_response("submit_contribution", {"contribution_markdown": "B."})],
            ),
        }
        orch = PanelOrchestrator(
            db,
            lambda m: services[m],
            {"embedded_tools": {}},
            status_callback=lambda t, d: None,
        )
        orch.run(cid, "u1")
        p1_tools = {t["name"] for c in captured["model-p1"] for t in (c.get("tools") or [])}
        p2_tools = {t["name"] for c in captured["model-p2"] for t in (c.get("tools") or [])}
        assert "submit_contribution" in p1_tools
        assert "get_current_datetime" not in p1_tools
        assert "get_current_datetime" in p2_tools
