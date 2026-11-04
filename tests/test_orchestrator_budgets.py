"""Debates and panels: every agent's model calls metered, the budget checked at each turn's start.

A run or a QA answer is refused at 100% before anything starts; a run that
reaches the budget part-way lets the speaker who has the floor finish, then
pauses before the next turn, resumable once the budget allows. Stub services
only.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.core.debate.orchestrator import DebateOrchestrator
from spark.core.panel.orchestrator import PanelOrchestrator
from spark.database import debates, usage
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.usage.budgets import BudgetExceeded
from tests.test_debate_orchestrator import (
    ScriptedService,
    _setup_debate,
    text_response,
    tool_response,
)
from tests.test_panel_orchestrator import _panel, _services

USER = "u1"


class CountingService(ScriptedService):
    def __init__(self, responses: list[dict]) -> None:
        super().__init__(responses)
        self.calls = 0

    def invoke_model(self, messages: Any, **kwargs: Any) -> dict:
        self.calls += 1
        return super().invoke_model(messages, **kwargs)


def _debate_services() -> dict[str, CountingService]:
    return {
        "model-judge": CountingService(
            [
                tool_response("set_speaking_order", {"first_speaker": "pro"}, "Welcome."),
                text_response("Pro wins."),
                text_response("An answer."),
            ]
        ),
        "model-pro": CountingService(
            [tool_response("submit_argument", {"argument_markdown": "Pro case."})]
        ),
        "model-con": CountingService(
            [tool_response("submit_argument", {"argument_markdown": "Con case."})]
        ),
    }


def _debate(sync_db: Any, services: dict, events: list) -> DebateOrchestrator:
    return DebateOrchestrator(
        sync_db, lambda m: services[m], {}, status_callback=lambda t, d: events.append((t, d))
    )


def _panel_orch(sync_db: Any, services: dict, events: list) -> PanelOrchestrator:
    return PanelOrchestrator(
        sync_db, lambda m: services[m], {}, status_callback=lambda t, d: events.append((t, d))
    )


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT user_guid, model_id, source, conversation_id, input_tokens, output_tokens"
        " FROM usage_tracking ORDER BY id"
    )


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


class TestDebate:
    def test_every_agents_calls_are_recorded_for_the_debates_user(self, sync_db) -> None:
        cid = _setup_debate(sync_db)
        _debate(sync_db, _debate_services(), []).run(cid, USER)
        rows = _rows(sync_db)
        assert [(r["model_id"], r["source"]) for r in rows] == [
            ("model-judge", "debate"),
            ("model-pro", "debate"),
            ("model-con", "debate"),
            ("model-judge", "debate"),
        ]
        assert {(r["user_guid"], r["conversation_id"]) for r in rows} == {(USER, cid)}
        assert sync_db.call(usage.tokens_in_period, USER, usage.period_of(_now())) == 60

    def test_a_run_past_the_budget_is_refused_before_it_starts(self, sync_db) -> None:
        cid = _setup_debate(sync_db)
        sync_db.call(usage.set_user_override, USER, 10)
        sync_db.call(usage.record_usage, None, "m", 10, 0, USER)
        services = _debate_services()
        with pytest.raises(BudgetExceeded):
            _debate(sync_db, services, []).run(cid, USER)
        assert sum(s.calls for s in services.values()) == 0
        assert sync_db.call(debates.get_turns, USER, cid) == []

    def test_the_speaker_finishes_then_the_debate_pauses(self, sync_db) -> None:
        cid = _setup_debate(sync_db)
        # The opening (15) and pro's argument (15) start under 20; con's turn does not.
        sync_db.call(usage.set_user_override, USER, 20)
        services = _debate_services()
        events: list = []
        _debate(sync_db, services, events).run(cid, USER)

        turns = sync_db.call(debates.get_turns, USER, cid)
        assert [(t["role"], t["status"]) for t in turns if t["turn_type"] == "argument"] == [
            ("pro", "complete")
        ]
        assert services["model-con"].calls == 0
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "round"
        [(_, refused)] = [e for e in events if e[0] == "budget_exceeded"]
        assert "monthly token budget" in refused["message"]
        assert not [e for e in events if e[0] == "turn_failed"]

        # Resumable once the budget allows.
        sync_db.call(usage.set_user_override, USER, None)
        _debate(sync_db, services, []).run(cid, USER)
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "qa"

    def test_a_qa_answer_past_the_budget_is_refused(self, sync_db) -> None:
        cid = _setup_debate(sync_db)
        services = _debate_services()
        _debate(sync_db, services, []).run(cid, USER)
        sync_db.call(usage.set_user_override, USER, 1)
        orch = _debate(sync_db, services, [])
        turn_id = orch.submit_user_prompt(cid, USER, "Why?")
        with pytest.raises(BudgetExceeded):
            orch.answer_qa(cid, USER, turn_id)

    def test_the_users_roles_decide_on_a_server(self, sync_db, server_mode) -> None:
        cid = _setup_debate(sync_db)
        sync_db.call(usage.set_role_budget, "user", 10)
        sync_db.call(usage.record_usage, None, "m", 10, 0, USER)
        with pytest.raises(BudgetExceeded):
            _debate(sync_db, _debate_services(), []).run(cid, USER, roles={"user"})
        # The same usage with a role that sets no budget is not limited.
        _debate(sync_db, _debate_services(), []).run(cid, USER, roles={"power_user"})
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "qa"


class TestPanel:
    def test_every_agents_calls_are_recorded_for_the_panels_user(self, sync_db) -> None:
        cid = _panel(sync_db)
        _panel_orch(sync_db, _services(["panellist:1", "panellist:2"]), []).run(cid, USER)
        rows = _rows(sync_db)
        assert {r["source"] for r in rows} == {"panel"}
        assert {(r["user_guid"], r["conversation_id"]) for r in rows} == {(USER, cid)}
        assert [r["model_id"] for r in rows] == ["model-mod", "model-p1", "model-p2", "model-mod"]

    def test_a_run_past_the_budget_is_refused_before_it_starts(self, sync_db) -> None:
        cid = _panel(sync_db)
        sync_db.call(usage.set_user_override, USER, 10)
        sync_db.call(usage.record_usage, None, "m", 10, 0, USER)
        with pytest.raises(BudgetExceeded):
            _panel_orch(sync_db, _services(["panellist:1", "panellist:2"]), []).run(cid, USER)
        assert sync_db.call(debates.get_turns, USER, cid) == []

    def test_the_panellist_finishes_then_the_panel_pauses(self, sync_db) -> None:
        cid = _panel(sync_db)
        sync_db.call(usage.set_user_override, USER, 20)
        services = _services(["panellist:1", "panellist:2"])
        events: list = []
        _panel_orch(sync_db, services, events).run(cid, USER)
        turns = sync_db.call(debates.get_turns, USER, cid)
        assert [t["role"] for t in turns if t["turn_type"] == "contribution"] == ["panellist:1"]
        assert [e for e in events if e[0] == "budget_exceeded"]
        assert sync_db.call(debates.get_debate, USER, cid)["state"] == "round"

    def test_a_qa_answer_past_the_budget_is_refused(self, sync_db) -> None:
        cid = _panel(sync_db)
        services = _services(["panellist:1", "panellist:2"])
        services["model-mod"].responses.append(text_response("An answer."))
        _panel_orch(sync_db, services, []).run(cid, USER)
        sync_db.call(usage.set_user_override, USER, 1)
        orch = _panel_orch(sync_db, services, [])
        turn_id = orch.submit_user_prompt(cid, USER, "Why?")
        with pytest.raises(BudgetExceeded):
            orch.answer_qa(cid, USER, turn_id)


def _now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
