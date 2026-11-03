"""Scheduled actions: metered for their owner, refused at 100% of the owner's budget (A14).

Personal mode here; the worker's server-mode runs, with owners and roles read
from warden, are in ``test_worker_actions.py``. Stub models only.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark.database import autonomous_actions, usage
from spark.scheduler.executor import ActionExecutor

OWNER = "local-owner"


class _Settings:
    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return {"embedded_tools": {}, "conversation.prompt_caching": False}.get(key, default)


class Ctx:
    settings = _Settings()
    secrets = None


class ScriptedLLM:
    """One tool call to an unknown tool, then a final answer."""

    def __init__(self, first: int = 10, second: int = 4) -> None:
        self.calls = 0
        self.first = first
        self.second = second

    def set_model(self, model_id: str) -> None:
        pass

    def invoke_model(self, messages: list[Any], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            return {
                "stop_reason": "tool_use",
                "content": "",
                "content_blocks": [],
                "usage": {"input_tokens": self.first, "output_tokens": 0},
                "tool_use": [{"id": "t1", "name": "no_such_tool", "input": {}}],
            }
        return {
            "stop_reason": "end_turn",
            "content": "done",
            "usage": {"input_tokens": self.second, "output_tokens": 1},
        }


@pytest.fixture()
def llm(monkeypatch: pytest.MonkeyPatch) -> list[ScriptedLLM]:
    made: list[ScriptedLLM] = []

    def _init(self: Any, model_id: str) -> ScriptedLLM:
        model = ScriptedLLM()
        made.append(model)
        return model

    monkeypatch.setattr(ActionExecutor, "_init_llm", _init)
    monkeypatch.setattr(ActionExecutor, "_init_mcp", lambda self: None)
    return made


def _action(sync_db: Any) -> int:
    return sync_db.call(
        autonomous_actions.create_action,
        "nightly",
        "write the report",
        "model-a",
        OWNER,
        schedule_type="recurring",
        schedule_config='{"cron": "0 3 * * *"}',
    )


def _execute(sync_db: Any, aid: int) -> None:
    ActionExecutor(Ctx(), "daemon-1", sync_db, user_guid=OWNER).execute(aid)


def _runs(sync_db: Any, aid: int) -> list[Any]:
    return sync_db.call(autonomous_actions.get_action_runs, OWNER, aid)


class TestMetering:
    def test_every_call_is_billed_to_the_owner_with_its_action(self, sync_db, llm) -> None:
        aid = _action(sync_db)
        _execute(sync_db, aid)
        rows = sync_db.fetch_all(
            "SELECT user_guid, model_id, source, action_id, conversation_id, input_tokens"
            " FROM usage_tracking ORDER BY id"
        )
        assert rows == [
            {
                "user_guid": OWNER,
                "model_id": "model-a",
                "source": "action",
                "action_id": aid,
                "conversation_id": None,
                "input_tokens": 10,
            },
            {
                "user_guid": OWNER,
                "model_id": "model-a",
                "source": "action",
                "action_id": aid,
                "conversation_id": None,
                "input_tokens": 4,
            },
        ]

    def test_the_run_record_keeps_its_totals(self, sync_db, llm) -> None:
        aid = _action(sync_db)
        _execute(sync_db, aid)
        [run] = _runs(sync_db, aid)
        assert (run["status"], run["input_tokens"], run["output_tokens"]) == ("completed", 14, 1)


class TestBudget:
    def test_no_budget_unless_set(self, sync_db, llm) -> None:
        sync_db.call(usage.record_usage, None, "m", 10**9, 0, OWNER)
        aid = _action(sync_db)
        _execute(sync_db, aid)
        assert [r["status"] for r in _runs(sync_db, aid)] == ["completed"]

    def test_at_100_percent_the_run_is_refused_and_recorded(self, sync_db, llm) -> None:
        sync_db.call(usage.set_user_override, OWNER, 50)
        sync_db.call(usage.record_usage, None, "m", 50, 0, OWNER)
        aid = _action(sync_db)
        _execute(sync_db, aid)
        [run] = _runs(sync_db, aid)
        assert run["status"] == "refused"
        assert "monthly token budget" in (run["error_message"] or run["result_text"] or "")
        assert llm == []

    def test_a_run_in_flight_finishes_past_the_budget(self, sync_db, llm) -> None:
        sync_db.call(usage.set_user_override, OWNER, 12)
        aid = _action(sync_db)
        _execute(sync_db, aid)  # 10, then 5 more: past 12, but the run had started
        assert [r["status"] for r in _runs(sync_db, aid)] == ["completed"]
        _execute(sync_db, aid)
        assert [r["status"] for r in _runs(sync_db, aid)] == ["refused", "completed"]
