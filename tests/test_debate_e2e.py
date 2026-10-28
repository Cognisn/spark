"""End-to-end debate: 2 rounds, judge-decides, ruling, QA, export."""

from __future__ import annotations

import json

import pytest

from spark.core.debate.export import export_debate_json, export_debate_markdown
from spark.core.debate.orchestrator import DebateOrchestrator
from spark.database import debates
from tests.test_debate_orchestrator import (
    AGENTS,
    ScriptedService,
    text_response,
    tool_response,
)


def _setup_debate(sync_db, rounds_mode="judge", max_rounds=None) -> int:
    cid = sync_db.fetch_value(
        "INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        "VALUES (?, ?, ?, 'debate') RETURNING id",
        ("D", "model-judge", "u1"),
    )
    sync_db.call(debates.create_debate, cid, "Topic T", rounds_mode, max_rounds, "u1", AGENTS)
    return cid


def test_two_round_judge_decides_debate(sync_db) -> None:
    cid = _setup_debate(sync_db)
    services = {
        "model-judge": ScriptedService(
            [
                tool_response("set_speaking_order", {"first_speaker": "con"}, "Open."),
                tool_response("request_next_round", {}, "More argument needed."),
                tool_response("deliver_ruling", {}, "I have heard enough."),
                text_response("Con wins on the evidence."),
                text_response("Because exhibit B was decisive."),
            ]
        ),
        "model-pro": ScriptedService(
            [
                tool_response("submit_argument", {"argument_markdown": "P1."}),
                tool_response("submit_argument", {"argument_markdown": "P2."}),
            ]
        ),
        "model-con": ScriptedService(
            [
                tool_response(
                    "submit_argument",
                    {
                        "argument_markdown": "C1.",
                        "exhibits": [{"title": "B", "content": "data"}],
                    },
                ),
                tool_response("submit_argument", {"argument_markdown": "C2."}),
            ]
        ),
    }
    orch = DebateOrchestrator(sync_db, lambda m: services[m], {}, status_callback=lambda t, d: None)
    orch.run(cid, "u1")

    d = sync_db.call(debates.get_debate, cid)
    assert d["state"] == "qa" and d["opening_speaker"] == "con"
    args = [t for t in sync_db.call(debates.get_turns, cid) if t["turn_type"] == "argument"]
    assert [a["role"] for a in args] == ["con", "pro", "con", "pro"]

    qid = orch.submit_user_prompt(cid, "Why con?")
    assert "exhibit B" in orch.answer_qa(cid, "u1", qid)

    md = export_debate_markdown(sync_db, cid)
    assert "## Round 2" in md and "Con wins" in md
    assert json.loads(export_debate_json(sync_db, cid))["state"] == "qa"
