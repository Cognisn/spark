"""Debates and panels: every agent's model calls metered, credits checked at each turn's start.

A run or a QA answer is refused at 100% before anything starts; a run that
reaches the budget part-way lets the speaker who has the floor finish, then
pauses before the next turn, resumable once the budget allows. The agents answer
from scripts on conduit's stub provider, through the real connection runtime, so
every usage row comes from conduit's events.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark.core.debate.orchestrator import DebateOrchestrator
from spark.core.panel.orchestrator import PanelOrchestrator
from spark.database import debates, usage
from spark.usage.budgets import BudgetExceeded
from tests.credit_helpers import allow, metered, server_mode, spent  # noqa: F401
from tests.model_helpers import ScriptedModels
from tests.test_debate_orchestrator import _setup_debate, text_response, tool_response
from tests.test_panel_orchestrator import _panel, _services

USER = "u1"


@pytest.fixture()
def made() -> Iterator[list[ScriptedModels]]:
    scripted: list[ScriptedModels] = []
    yield scripted
    for models in scripted:
        models.close()


def _debate_services(sync_db: Any, made: list[ScriptedModels]) -> ScriptedModels:
    models = ScriptedModels(
        sync_db,
        {
            "model-judge": [
                tool_response("set_speaking_order", {"first_speaker": "pro"}, "Welcome."),
                text_response("Pro wins."),
                text_response("An answer."),
            ],
            "model-pro": [tool_response("submit_argument", {"argument_markdown": "Pro case."})],
            "model-con": [tool_response("submit_argument", {"argument_markdown": "Con case."})],
        },
    )
    made.append(models)
    return models


def _panel_services(sync_db: Any, made: list[ScriptedModels]) -> ScriptedModels:
    scripted = _services(["panellist:1", "panellist:2"])
    models = ScriptedModels(sync_db, {m: s.responses for m, s in scripted.items()})
    made.append(models)
    return models


def _debate(sync_db: Any, models: ScriptedModels, events: list) -> DebateOrchestrator:
    return DebateOrchestrator(
        sync_db, models.factory, {}, status_callback=lambda t, d: events.append((t, d))
    )


def _panel_orch(sync_db: Any, models: ScriptedModels, events: list) -> PanelOrchestrator:
    return PanelOrchestrator(
        sync_db, models.factory, {}, status_callback=lambda t, d: events.append((t, d))
    )


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT user_guid, model_id, source, conversation_id, input_tokens, output_tokens"
        " FROM usage_tracking ORDER BY id"
    )


class TestDebate:
    def test_every_agents_calls_are_recorded_for_the_debates_user(self, sync_db, made) -> None:
        cid = _setup_debate(sync_db)
        _debate(sync_db, _debate_services(sync_db, made), []).run(cid, USER)
        rows = _rows(sync_db)
        assert [(r["model_id"], r["source"]) for r in rows] == [
            ("model-judge", "debate"),
            ("model-pro", "debate"),
            ("model-con", "debate"),
            ("model-judge", "debate"),
        ]
        assert {(r["user_guid"], r["conversation_id"]) for r in rows} == {(USER, cid)}
        assert sync_db.call(usage.tokens_in_period, USER, usage.period_of(_now())) == 60

    def test_a_run_past_the_allowance_is_refused_before_it_starts(
        self, sync_db, made, server_mode
    ) -> None:
        cid = _setup_debate(sync_db)
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=10)
        sync_db.call(spent, USER, 10)
        services = _debate_services(sync_db, made)
        with pytest.raises(BudgetExceeded):
            _debate(sync_db, services, []).run(cid, USER)
        assert sum(services.calls.values()) == 0
        assert sync_db.call(debates.get_turns, USER, cid) == []

    def test_the_speaker_finishes_then_the_debate_pauses(self, sync_db, made, server_mode) -> None:
        cid = _setup_debate(sync_db)
        # The opening (15) and pro's argument (15) start under 20; con's turn does not.
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=20)
        services = _debate_services(sync_db, made)
        events: list = []
        _debate(sync_db, services, events).run(cid, USER)

        turns = sync_db.call(debates.get_turns, USER, cid)
        assert [(t["role"], t["status"]) for t in turns if t["turn_type"] == "argument"] == [
            ("pro", "complete")
        ]
        assert services.calls["model-con"] == 0
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "round"
        [(_, refused)] = [e for e in events if e[0] == "budget_exceeded"]
        assert "monthly credits on Stub stub" in refused["message"]
        assert refused["connection_id"] == "stub"
        assert not [e for e in events if e[0] == "turn_failed"]

        # Resumable once the allowance allows.
        sync_db.call(allow, user=USER, credits=None)
        _debate(sync_db, services, []).run(cid, USER)
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "qa"

    def test_a_qa_answer_past_the_allowance_is_refused(self, sync_db, made, server_mode) -> None:
        cid = _setup_debate(sync_db)
        services = _debate_services(sync_db, made)
        _debate(sync_db, services, []).run(cid, USER)
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=1)
        sync_db.call(spent, USER, 1)
        orch = _debate(sync_db, services, [])
        turn_id = orch.submit_user_prompt(cid, USER, "Why?")
        with pytest.raises(BudgetExceeded):
            orch.answer_qa(cid, USER, turn_id)

    def test_the_users_roles_decide_on_a_server(self, sync_db, made, server_mode) -> None:
        cid = _setup_debate(sync_db)
        sync_db.call(metered)
        sync_db.call(allow, role="user", credits=10)
        sync_db.call(allow, role="power_user", credits=None)
        sync_db.call(spent, USER, 10)
        with pytest.raises(BudgetExceeded):
            _debate(sync_db, _debate_services(sync_db, made), []).run(cid, USER, roles={"user"})
        # The same usage with a role that sets no limit is not limited.
        _debate(sync_db, _debate_services(sync_db, made), []).run(cid, USER, roles={"power_user"})
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "qa"

    def test_personal_mode_never_refuses(self, sync_db, made) -> None:
        cid = _setup_debate(sync_db)
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=1)
        sync_db.call(spent, USER, 10**6)
        _debate(sync_db, _debate_services(sync_db, made), []).run(cid, USER)
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "qa"


class TestPanel:
    def test_every_agents_calls_are_recorded_for_the_panels_user(self, sync_db, made) -> None:
        cid = _panel(sync_db)
        _panel_orch(sync_db, _panel_services(sync_db, made), []).run(cid, USER)
        rows = _rows(sync_db)
        assert {r["source"] for r in rows} == {"panel"}
        assert {(r["user_guid"], r["conversation_id"]) for r in rows} == {(USER, cid)}
        assert [r["model_id"] for r in rows] == ["model-mod", "model-p1", "model-p2", "model-mod"]

    def test_a_run_past_the_allowance_is_refused_before_it_starts(
        self, sync_db, made, server_mode
    ) -> None:
        cid = _panel(sync_db)
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=10)
        sync_db.call(spent, USER, 10)
        with pytest.raises(BudgetExceeded):
            _panel_orch(sync_db, _panel_services(sync_db, made), []).run(cid, USER)
        assert sync_db.call(debates.get_turns, USER, cid) == []

    def test_the_panellist_finishes_then_the_panel_pauses(self, sync_db, made, server_mode) -> None:
        cid = _panel(sync_db)
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=20)
        services = _panel_services(sync_db, made)
        events: list = []
        _panel_orch(sync_db, services, events).run(cid, USER)
        turns = sync_db.call(debates.get_turns, USER, cid)
        assert [t["role"] for t in turns if t["turn_type"] == "contribution"] == ["panellist:1"]
        assert [e for e in events if e[0] == "budget_exceeded"]
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "round"

    def test_a_qa_answer_past_the_allowance_is_refused(self, sync_db, made, server_mode) -> None:
        cid = _panel(sync_db)
        services = _panel_services(sync_db, made)
        services.responses["model-mod"].append(text_response("An answer."))
        _panel_orch(sync_db, services, []).run(cid, USER)
        sync_db.call(metered)
        sync_db.call(allow, user=USER, credits=1)
        sync_db.call(spent, USER, 1)
        orch = _panel_orch(sync_db, services, [])
        turn_id = orch.submit_user_prompt(cid, USER, "Why?")
        with pytest.raises(BudgetExceeded):
            orch.answer_qa(cid, USER, turn_id)


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
