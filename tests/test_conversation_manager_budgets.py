"""Chat turns: every model call metered for the turn's user, the budget checked at turn start (A14).

The stub providers answer from memory; no model is ever called.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest

from spark import mode
from spark.core.conversation_manager import ConversationManager
from spark.database import usage
from spark.llm.context_limits import ContextLimitResolver
from spark.llm.manager import LLMManager
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.usage.budgets import BudgetExceeded
from tests.test_conversation_manager import StubLLMService

ADA = "user-ada-example"
GRACE = "user-grace-example"
PERIOD = datetime.now(timezone.utc).strftime("%Y-%m")
AGENTS_ON = {"embedded_tools": {"agents": {"enabled": True, "default_mode": "orchestrator"}}}


def _text(content: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "tool_use": None,
        "content_blocks": [{"type": "text", "text": content}],
    }


def _tool_call(name: str, tool_input: dict[str, Any], tokens: int) -> dict[str, Any]:
    call = {"type": "tool_use", "id": f"call-{name}", "name": name, "input": tool_input}
    return {
        "content": "",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": tokens, "output_tokens": 0},
        "tool_use": [call],
        "content_blocks": [call],
    }


class ScriptedLLM(StubLLMService):
    """Answers each call from a script shared by every copy, then with plain text."""

    def __init__(self) -> None:
        super().__init__()
        self.script: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.calls = 0
        self.before_call: Any = None

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        if self.before_call is not None:
            self.before_call()
        with self.lock:
            self.calls += 1
            if self.script:
                return self.script.pop(0)
        return _text("done", 30, 20)


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


def _manager(db: Any, config: dict[str, Any] | None = None) -> tuple[ConversationManager, Any]:
    stub = ScriptedLLM()
    llm = LLMManager()
    llm.register_provider(stub)  # type: ignore[arg-type]
    mgr = ConversationManager(
        db,
        llm,
        ContextLimitResolver(),
        max_tool_iterations=5,
        user_guid=ADA,
        embedded_tools_config=config,
    )
    mgr._turn_service = lambda model_id: stub  # type: ignore[method-assign]
    mgr._get_llm_service_for_model = lambda model_id: stub  # type: ignore[method-assign]
    return mgr, stub


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT user_guid, model_id, source, conversation_id, input_tokens, output_tokens"
        " FROM usage_tracking ORDER BY id"
    )


class TestRecording:
    def test_a_turn_records_its_model_call_for_its_user(self, sync_db) -> None:
        mgr, _ = _manager(sync_db)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        mgr.send_message(cid, "hello", ADA)
        assert _rows(sync_db) == [
            {
                "user_guid": ADA,
                "model_id": "stub-model",
                "source": "chat",
                "conversation_id": cid,
                "input_tokens": 30,
                "output_tokens": 20,
            }
        ]

    def test_every_call_of_a_tool_loop_is_recorded(self, sync_db) -> None:
        mgr, stub = _manager(sync_db)
        stub.script = [_tool_call("no_such_tool", {}, 11)]
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        mgr.send_message(cid, "hello", ADA)
        assert [(r["source"], r["input_tokens"]) for r in _rows(sync_db)] == [
            ("chat", 11),
            ("chat", 30),
        ]

    def test_a_sub_agents_calls_are_billed_to_the_turns_user(self, sync_db) -> None:
        mgr, stub = _manager(sync_db, AGENTS_ON)
        stub.script = [
            _tool_call("spawn_agent", {"task": "research", "agent_name": "Researcher"}, 5),
            _text("agent finding", 7, 3),  # the sub-agent's own call
        ]
        cid = mgr.create_conversation("Grace's", "stub-model", GRACE)
        mgr.send_message(cid, "go", GRACE)
        rows = _rows(sync_db)
        assert {r["user_guid"] for r in rows} == {GRACE}
        assert ("agent", 7, 3) in [
            (r["source"], r["input_tokens"], r["output_tokens"]) for r in rows
        ]

    def test_compaction_is_recorded_as_compaction(self, sync_db, monkeypatch) -> None:
        mgr, _ = _manager(sync_db)
        seen: list[Any] = []

        def compact(*args: Any, service: Any = None, **kwargs: Any) -> None:
            seen.append(service)
            service.invoke_model([])

        assert mgr._compactor is not None
        monkeypatch.setattr(mgr._compactor, "check_and_compact", compact)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        mgr.send_message(cid, "hello", ADA)
        assert [(r["source"], r["user_guid"]) for r in _rows(sync_db)] == [
            ("chat", ADA),
            ("compaction", ADA),
        ]

    def test_a_locked_compaction_model_is_metered_too(self, sync_db, monkeypatch) -> None:
        mgr, stub = _manager(sync_db)
        assert mgr._compactor is not None
        monkeypatch.setattr(mgr._compactor, "_locked_model", "locked-model")
        monkeypatch.setattr(mgr._llm, "service_for", lambda model_id: stub)
        monkeypatch.setattr(
            mgr._compactor,
            "check_and_compact",
            lambda *a, service=None, **k: service.invoke_model([]),
        )
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        mgr.send_message(cid, "hello", ADA)
        assert [(r["source"], r["model_id"]) for r in _rows(sync_db)] == [
            ("chat", "stub-model"),
            ("compaction", "locked-model"),
        ]

    def test_personal_mode_records_and_never_refuses_without_a_budget(self, sync_db) -> None:
        mgr, _ = _manager(sync_db)
        sync_db.call(usage.record_usage, None, "stub-model", 10**9, 0, ADA)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        result = mgr.send_message(cid, "hello", ADA)
        assert result["status"] == "completed"
        assert result["budget"]["state"] == "unlimited"

    def test_the_conversation_counters_are_unchanged(self, sync_db) -> None:
        from spark.database import conversations

        mgr, _ = _manager(sync_db)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        mgr.send_message(cid, "hello", ADA)
        conv = sync_db.call(conversations.get_conversation, cid, ADA)
        assert (conv["tokens_sent"], conv["tokens_received"]) == (30, 20)


class TestBudgetAtTurnStart:
    def test_at_100_percent_a_new_turn_is_refused_before_anything_runs(
        self, sync_db, server_mode
    ) -> None:
        from spark.database import messages

        mgr, stub = _manager(sync_db)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.record_usage, None, "stub-model", 60, 40, ADA)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        with pytest.raises(BudgetExceeded, match="monthly token budget"):
            mgr.send_message(cid, "hello", ADA, roles={"user"})
        assert stub.calls == 0
        assert sync_db.call(messages.get_messages, ADA, cid) == []

    def test_the_users_override_lifts_the_role_budget(self, sync_db, server_mode) -> None:
        mgr, _ = _manager(sync_db)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.set_user_override, ADA, None)
        sync_db.call(usage.record_usage, None, "stub-model", 500, 0, ADA)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        assert mgr.send_message(cid, "hello", ADA, roles={"user"})["status"] == "completed"

    def test_another_users_spending_does_not_count(self, sync_db, server_mode) -> None:
        mgr, _ = _manager(sync_db)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.record_usage, None, "stub-model", 500, 0, GRACE)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        assert mgr.send_message(cid, "hello", ADA, roles={"user"})["status"] == "completed"

    def test_the_turn_reports_the_80_percent_warning(self, sync_db, server_mode) -> None:
        mgr, _ = _manager(sync_db)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.record_usage, None, "stub-model", 30, 0, ADA)
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        result = mgr.send_message(cid, "hello", ADA, roles={"user"})  # +50: 80 of 100
        assert result["budget"]["state"] == "warning"
        assert result["budget"]["used"] == 80

    @pytest.mark.parametrize("budget", ["role", "override"])
    def test_an_in_flight_turn_finishes_past_the_budget(
        self, sync_db, server_mode, budget: str
    ) -> None:
        mgr, stub = _manager(sync_db)
        if budget == "role":
            sync_db.call(usage.set_role_budget, "user", 100)
        else:
            sync_db.call(usage.set_user_override, ADA, 100)
        # The first call alone passes 100%, so the turn's next call is made over budget.
        stub.script = [_tool_call("no_such_tool", {}, 120), _text("finished", 40, 10)]
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        result = mgr.send_message(cid, "hello", ADA, roles={"user"})
        assert result["status"] == "completed"
        assert result["content"] == "finished"
        assert stub.calls == 2
        assert (result["budget"]["state"], result["budget"]["used"]) == ("exceeded", 170)
        with pytest.raises(BudgetExceeded):
            mgr.send_message(cid, "and again", ADA, roles={"user"})


@pytest.fixture()
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    """The bridge over a fresh database at head, on SQLite and (marked) PostgreSQL."""
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


class TestParallelTurns:
    """One user's turns running at once, on both engines (A17)."""

    TURNS = 4

    def test_parallel_turns_are_all_counted_and_all_finish(self, bridge, server_mode) -> None:
        mgr, stub = _manager(bridge)
        bridge.call(usage.set_role_budget, "user", 100)
        # Every turn waits until all are in flight, so each passed the check under budget.
        in_flight = threading.Barrier(self.TURNS, timeout=30)
        stub.before_call = in_flight.wait
        conversations = [
            mgr.create_conversation(f"chat {n}", "stub-model", ADA) for n in range(self.TURNS)
        ]

        def turn(cid: int) -> dict[str, Any]:
            return mgr.send_message(cid, "hello", ADA, roles={"user"})

        with ThreadPoolExecutor(self.TURNS) as pool:
            results = list(pool.map(turn, conversations))

        assert [r["status"] for r in results] == ["completed"] * self.TURNS
        # Each turn used 50 tokens: 200 recorded exactly, past the budget of 100.
        assert bridge.call(usage.tokens_in_period, ADA, PERIOD) == 50 * self.TURNS
        stub.before_call = None
        with pytest.raises(BudgetExceeded):
            mgr.send_message(conversations[0], "one more", ADA, roles={"user"})
