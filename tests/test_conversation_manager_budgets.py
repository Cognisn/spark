"""Chat turns: every model call metered for the turn's user, the budget checked at turn start (A14).

The turns run on the real connection runtime over conduit's stub provider: no model
is ever called, and every usage row comes from conduit's ``model_call_finished``
events through SPARK's usage sink.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest
from conduit import ModelRequest, Usage
from conduit.providers.stub import StubToolCall, StubTurn

from spark import mode
from spark.connections import ConnectionRuntime
from spark.core.conversation_manager import ConversationManager
from spark.database import usage
from spark.database.connections import Assignment
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.usage.budgets import BudgetExceeded
from tests.model_helpers import stub_connection, stub_runtime

ADA = "user-ada-example"
GRACE = "user-grace-example"
PERIOD = datetime.now(timezone.utc).strftime("%Y-%m")
AGENTS_ON = {"embedded_tools": {"agents": {"enabled": True, "default_mode": "orchestrator"}}}
MODEL = "stub/stub-echo"


def _usage(input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def _text(content: str, input_tokens: int, output_tokens: int) -> StubTurn:
    return StubTurn(text=content, usage=_usage(input_tokens, output_tokens))


def _tool_call(name: str, tool_input: dict[str, Any], tokens: int) -> StubTurn:
    return StubTurn(
        tool_calls=(StubToolCall(name=name, input=tool_input),), usage=_usage(tokens, 0)
    )


class Script:
    """Answers each call from a list of turns, then with plain text (30 in, 20 out).

    It runs on conduit's loop, one call at a time, so it never blocks."""

    def __init__(self) -> None:
        self.turns: list[StubTurn] = []
        self.calls = 0

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        self.calls += 1
        if self.turns:
            return self.turns.pop(0)
        return _text("done", 30, 20)


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


@pytest.fixture()
def made() -> Iterator[list[ConnectionRuntime]]:
    runtimes: list[ConnectionRuntime] = []
    yield runtimes
    for runtime in runtimes:
        runtime.close()


def _manager(
    db: Any, made: list[ConnectionRuntime] | None = None, config: dict[str, Any] | None = None
) -> tuple[ConversationManager, Script]:
    """A manager on a stub connection assigned to every role. Its runtime is added to
    ``made`` for the test to close (or is the manager's ``_models``)."""
    script = Script()
    runtime = stub_runtime(
        db,
        connections=[stub_connection(script="budget")],
        scripts={"budget": script},
        assignments=[
            Assignment("stub", role=role) for role in ("user", "power_user", "administrator")
        ],
    )
    if made is not None:
        made.append(runtime)
    mgr = ConversationManager(
        db, runtime, max_tool_iterations=5, user_guid=ADA, embedded_tools_config=config
    )
    return mgr, script


def _rows(db: Any) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT user_guid, connection_id, model_id, source, conversation_id, input_tokens,"
        " output_tokens FROM usage_tracking ORDER BY id"
    )


class TestRecording:
    def test_a_turn_records_its_model_call_for_its_user(self, sync_db: Any, made: Any) -> None:
        mgr, _ = _manager(sync_db, made)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        mgr.send_message(cid, "hello", ADA)
        assert _rows(sync_db) == [
            {
                "user_guid": ADA,
                "connection_id": "stub",
                "model_id": "stub-echo",
                "source": "chat",
                "conversation_id": cid,
                "input_tokens": 30,
                "output_tokens": 20,
            }
        ]

    def test_every_call_of_a_tool_loop_is_recorded(self, sync_db: Any, made: Any) -> None:
        mgr, script = _manager(sync_db, made)
        script.turns = [_tool_call("no_such_tool", {}, 11)]
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        mgr.send_message(cid, "hello", ADA)
        assert [(r["source"], r["input_tokens"]) for r in _rows(sync_db)] == [
            ("chat", 11),
            ("chat", 30),
        ]

    def test_a_sub_agents_calls_are_billed_to_the_turns_user(self, sync_db: Any, made: Any) -> None:
        mgr, script = _manager(sync_db, made, AGENTS_ON)
        script.turns = [
            _tool_call("spawn_agent", {"task": "research", "agent_name": "Researcher"}, 5),
            _text("agent finding", 7, 3),  # the sub-agent's own call
        ]
        cid = mgr.create_conversation("Grace's", MODEL, GRACE)
        mgr.send_message(cid, "go", GRACE)
        rows = _rows(sync_db)
        assert {r["user_guid"] for r in rows} == {GRACE}
        assert ("agent", 7, 3) in [
            (r["source"], r["input_tokens"], r["output_tokens"]) for r in rows
        ]

    def test_compaction_is_recorded_as_compaction(
        self, sync_db: Any, made: Any, monkeypatch: Any
    ) -> None:
        mgr, _ = _manager(sync_db, made)

        def compact(*args: Any, service: Any = None, **kwargs: Any) -> None:
            service.invoke_model([{"role": "user", "content": "summarise"}])

        assert mgr._compactor is not None
        monkeypatch.setattr(mgr._compactor, "check_and_compact", compact)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        mgr.send_message(cid, "hello", ADA)
        assert [(r["source"], r["user_guid"]) for r in _rows(sync_db)] == [
            ("chat", ADA),
            ("compaction", ADA),
        ]

    def test_a_locked_compaction_model_is_metered_too(
        self, sync_db: Any, made: Any, monkeypatch: Any
    ) -> None:
        mgr, _ = _manager(sync_db, made)
        assert mgr._compactor is not None
        monkeypatch.setattr(mgr._compactor, "_locked_model", MODEL)
        monkeypatch.setattr(
            mgr._compactor,
            "check_and_compact",
            lambda *a, service=None, **k: service.invoke_model([{"role": "user", "content": "s"}]),
        )
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        mgr.send_message(cid, "hello", ADA)
        assert [(r["source"], r["model_id"], r["user_guid"]) for r in _rows(sync_db)] == [
            ("chat", "stub-echo", ADA),
            ("compaction", "stub-echo", ADA),
        ]

    def test_personal_mode_records_and_never_refuses_without_a_budget(
        self, sync_db: Any, made: Any
    ) -> None:
        mgr, _ = _manager(sync_db, made)
        sync_db.call(usage.record_usage, None, "stub-echo", 10**9, 0, ADA)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        result = mgr.send_message(cid, "hello", ADA)
        assert result["status"] == "completed"
        assert result["budget"]["state"] == "unlimited"

    def test_the_conversation_counters_are_unchanged(self, sync_db: Any, made: Any) -> None:
        from spark.database import conversations

        mgr, _ = _manager(sync_db, made)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        mgr.send_message(cid, "hello", ADA)
        conv = sync_db.call(conversations.get_conversation, cid, ADA)
        assert (conv["tokens_sent"], conv["tokens_received"]) == (30, 20)


class TestBudgetAtTurnStart:
    def test_at_100_percent_a_new_turn_is_refused_before_anything_runs(
        self, sync_db: Any, made: Any, server_mode: None
    ) -> None:
        from spark.database import messages

        mgr, script = _manager(sync_db, made)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.record_usage, None, "stub-echo", 60, 40, ADA)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        with pytest.raises(BudgetExceeded, match="monthly token budget"):
            mgr.send_message(cid, "hello", ADA, roles={"user"})
        assert script.calls == 0
        assert sync_db.call(messages.get_messages, ADA, cid) == []

    def test_the_users_override_lifts_the_role_budget(
        self, sync_db: Any, made: Any, server_mode: None
    ) -> None:
        mgr, _ = _manager(sync_db, made)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.set_user_override, ADA, None)
        sync_db.call(usage.record_usage, None, "stub-echo", 500, 0, ADA)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        assert mgr.send_message(cid, "hello", ADA, roles={"user"})["status"] == "completed"

    def test_another_users_spending_does_not_count(
        self, sync_db: Any, made: Any, server_mode: None
    ) -> None:
        mgr, _ = _manager(sync_db, made)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.record_usage, None, "stub-echo", 500, 0, GRACE)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        assert mgr.send_message(cid, "hello", ADA, roles={"user"})["status"] == "completed"

    def test_the_turn_reports_the_80_percent_warning(
        self, sync_db: Any, made: Any, server_mode: None
    ) -> None:
        mgr, _ = _manager(sync_db, made)
        sync_db.call(usage.set_role_budget, "user", 100)
        sync_db.call(usage.record_usage, None, "stub-echo", 30, 0, ADA)
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        result = mgr.send_message(cid, "hello", ADA, roles={"user"})  # +50: 80 of 100
        assert result["budget"]["state"] == "warning"
        assert result["budget"]["used"] == 80

    @pytest.mark.parametrize("budget", ["role", "override"])
    def test_an_in_flight_turn_finishes_past_the_budget(
        self, sync_db: Any, made: Any, server_mode: None, budget: str
    ) -> None:
        mgr, script = _manager(sync_db, made)
        if budget == "role":
            sync_db.call(usage.set_role_budget, "user", 100)
        else:
            sync_db.call(usage.set_user_override, ADA, 100)
        # The first call alone passes 100%, so the turn's next call is made over budget.
        script.turns = [_tool_call("no_such_tool", {}, 120), _text("finished", 40, 10)]
        cid = mgr.create_conversation("Ada's", MODEL, ADA)
        result = mgr.send_message(cid, "hello", ADA, roles={"user"})
        assert result["status"] == "completed"
        assert result["content"] == "finished"
        assert script.calls == 2
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

    def test_parallel_turns_are_all_counted_and_all_finish(
        self, bridge: Any, made: Any, server_mode: None, monkeypatch: Any
    ) -> None:
        mgr, _ = _manager(bridge, made)
        bridge.call(usage.set_role_budget, "user", 100)
        # Every turn waits, after its budget check, until all are in flight, so each
        # passed the check under budget before any model call was recorded.
        in_flight = threading.Barrier(self.TURNS, timeout=30)
        take_service = mgr._turn_service

        def after_the_check(*args: Any, **kwargs: Any) -> Any:
            in_flight.wait()
            return take_service(*args, **kwargs)

        monkeypatch.setattr(mgr, "_turn_service", after_the_check)
        conversations = [
            mgr.create_conversation(f"chat {n}", MODEL, ADA) for n in range(self.TURNS)
        ]

        def turn(cid: int) -> dict[str, Any]:
            return mgr.send_message(cid, "hello", ADA, roles={"user"})

        with ThreadPoolExecutor(self.TURNS) as pool:
            results = list(pool.map(turn, conversations))

        assert [r["status"] for r in results] == ["completed"] * self.TURNS
        # Each turn used 50 tokens: 200 recorded exactly, past the budget of 100.
        assert bridge.call(usage.tokens_in_period, ADA, PERIOD) == 50 * self.TURNS
        monkeypatch.setattr(mgr, "_turn_service", take_service)
        with pytest.raises(BudgetExceeded):
            mgr.send_message(conversations[0], "one more", ADA, roles={"user"})
