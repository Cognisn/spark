"""Each chat turn answers through a model service of its own, on its conversation's connection.

One connection runtime and one conversation manager serve every user of a server.
A conversation's model is a (connection, model) pair; each turn takes a service of
its own for it and uses that service for every call the turn makes (the tool loop,
token counts, prompt caching and compaction), billed to the turn's user. Nothing
shared changes with one user's choice, so turns on different connections at once
never answer with each other's. These tests run the real runtime on conduit's stub
provider (no network).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from conduit import ModelRequest
from conduit.providers.stub import StubToolCall, StubTurn

from spark import mode
from spark.connections import ConnectionRuntime
from spark.core.context_compaction import ContextCompactor
from spark.core.conversation_manager import ConversationManager
from spark.database import conversations
from spark.database.connections import Assignment
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.model_helpers import FixedLimits, stub_connection, stub_runtime
from tests.test_conversation_manager import StubLLMService

ADA = "user-ada-example"
GRACE = "user-grace-example"
ALPHA = "alpha/stub-echo"
BETA = "beta/stub-echo"


def _answer(name: str) -> Any:
    def script(request: ModelRequest, index: int) -> StubTurn:
        return StubTurn(text=f"answer from {name}")

    return script


def _tool_then_answer(request: ModelRequest, index: int) -> StubTurn:
    if index % 2 == 0:
        return StubTurn(tool_calls=(StubToolCall(name="no_such_tool"),))
    return StubTurn(text="done")


@pytest.fixture(params=["personal", "server"])
def either_mode(request: pytest.FixtureRequest) -> Iterator[str]:
    """Personal and server mode run the same turn code."""
    if request.param == "server":
        mode.configure(
            RuntimeSettings(
                Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y")
            )
        )
    else:
        mode.reset()
    yield request.param
    mode.reset()


def _conversation(sync_db: Any, user: str, model: str) -> int:
    """A plain conversation: no memory or retrieval (no embedding model needed)."""
    cid: int = sync_db.call(conversations.create_conversation, f"{user}'s", model, user)
    sync_db.call(conversations.update_conversation, cid, user, memory_enabled=0, rag_enabled=0)
    return cid


STAFF = ["staff"]


def _runtime(sync_db: Any, scripts: dict[str, Any]) -> ConnectionRuntime:
    """Stub connections named after their scripts, each assigned to the staff role."""
    return stub_runtime(
        sync_db,
        connections=[stub_connection(name, script=name) for name in sorted(scripts)],
        scripts=scripts,
        assignments=[Assignment(name, role="staff") for name in scripts],
    )


def _manager(sync_db: Any, runtime: ConnectionRuntime, **kwargs: Any) -> ConversationManager:
    return ConversationManager(sync_db, runtime, user_guid="process-guid", **kwargs)


def _usage(sync_db: Any) -> list[tuple[Any, ...]]:
    rows = sync_db.fetch_all(
        "SELECT user_guid, connection_id, model_id, source FROM usage_tracking ORDER BY id"
    )
    return [(r["user_guid"], r["connection_id"], r["model_id"], r["source"]) for r in rows]


class TestConcurrentTurns:
    def test_two_users_on_different_connections_each_get_their_own(
        self, sync_db: Any, either_mode: str
    ) -> None:
        start = threading.Barrier(2, timeout=30)
        runtime = _runtime(sync_db, {"alpha": _answer("alpha"), "beta": _answer("beta")})
        try:
            mgr = _manager(sync_db, runtime)
            chosen = {ADA: ALPHA, GRACE: BETA}
            cids = {user: _conversation(sync_db, user, m) for user, m in chosen.items()}
            answers: dict[str, Any] = {}

            def turn(user: str) -> None:
                start.wait()
                answers[user] = mgr.send_message(
                    cids[user], f"hello from {user}", user, tools=[], roles=STAFF
                )

            threads = [threading.Thread(target=turn, args=(user,)) for user in chosen]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        finally:
            runtime.close()

        assert answers[ADA]["content"] == "answer from alpha"
        assert answers[GRACE]["content"] == "answer from beta"
        assert sorted(_usage(sync_db)) == [
            (ADA, "alpha", "stub-echo", "chat"),
            (GRACE, "beta", "stub-echo", "chat"),
        ]


class TestOneTurn:
    def test_every_call_of_the_turn_uses_its_model(self, sync_db: Any, either_mode: str) -> None:
        runtime = _runtime(sync_db, {"alpha": _answer("alpha"), "beta": _tool_then_answer})
        try:
            mgr = _manager(sync_db, runtime)
            compacted: list[Any] = []
            assert mgr._compactor is not None

            def check_and_compact(*args: Any, service: Any = None, **kwargs: Any) -> bool:
                compacted.append(service)
                return False

            mgr._compactor.check_and_compact = check_and_compact  # type: ignore[method-assign]
            cid = _conversation(sync_db, ADA, BETA)

            result = mgr.send_message(cid, "hello", ADA, tools=[], roles=STAFF)
        finally:
            runtime.close()

        assert result["content"] == "done"
        assert result["iterations"] == 2
        assert _usage(sync_db) == [
            (ADA, "beta", "stub-echo", "chat"),
            (ADA, "beta", "stub-echo", "chat"),
        ]
        (service,) = compacted
        assert service.current_model == BETA
        assert service.attribution.source == "compaction"

    def test_a_bare_model_from_before_connections_still_answers(
        self, sync_db: Any, either_mode: str
    ) -> None:
        runtime = _runtime(sync_db, {"alpha": _answer("alpha"), "beta": _answer("beta")})
        try:
            mgr = _manager(sync_db, runtime)
            cid = _conversation(sync_db, ADA, "stub-echo")  # as 0.2 stored it
            answer = mgr.send_message(cid, "hello", ADA, tools=[], roles=STAFF)["content"]
            assert answer == "answer from alpha"
        finally:
            runtime.close()

    def test_a_model_no_connection_offers_is_reported(self, sync_db: Any, either_mode: str) -> None:
        runtime = _runtime(sync_db, {"alpha": _answer("alpha")})
        try:
            mgr = _manager(sync_db, runtime)
            cid = _conversation(sync_db, ADA, "gone/model-gone")
            result = mgr.send_message(cid, "hello", ADA, tools=[], roles=STAFF)
        finally:
            runtime.close()
        assert result["error"] is True
        assert "not available" in result["content"]
        assert _usage(sync_db) == []

    def test_the_connections_models_are_listed_for_sub_agents(self, sync_db: Any) -> None:
        runtime = _runtime(sync_db, {"alpha": _answer("alpha"), "beta": _answer("beta")})
        try:
            mgr = _manager(sync_db, runtime)
            text, is_error = mgr._list_provider_models(BETA)
        finally:
            runtime.close()
        assert not is_error
        assert "Stub beta" in text and BETA in text and ALPHA not in text


class TestServiceForModel:
    def test_each_call_gets_its_own_service(self, sync_db: Any) -> None:
        runtime = _runtime(sync_db, {"alpha": _answer("alpha"), "beta": _answer("beta")})
        try:
            mgr = _manager(sync_db, runtime)
            a = mgr._get_llm_service_for_model(ALPHA)
            b = mgr._get_llm_service_for_model(BETA)
            assert a is not None and b is not None and a is not b
            assert (a.current_model, b.current_model) == (ALPHA, BETA)
            assert mgr._get_llm_service_for_model("gone/model-gone") is None
            assert mgr._get_llm_service_for_model("") is None
        finally:
            runtime.close()


class TestCompactorService:
    def test_compacts_with_the_service_it_is_given(self, sync_db: Any) -> None:
        from spark.database import messages

        def _text(text: str) -> dict[str, Any]:
            return {
                "content": text,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "tool_use": None,
                "content_blocks": [{"type": "text", "text": text}],
            }

        own = StubLLMService()
        given = StubLLMService()
        given.responses = [_text("Summary of the prior conversation: " + ("blah " * 60))]
        compactor = ContextCompactor(own, sync_db, FixedLimits(), threshold=0.01)  # type: ignore[arg-type]
        cid = sync_db.call(conversations.create_conversation, "Test", "stub-model", ADA)
        # conduit's cut keeps the last assistant turn, so the conversation takes turns
        for i in range(4):
            role = "user" if i % 2 == 0 else "assistant"
            sync_db.call(messages.add_message, cid, role, f"message {i}", 50, ADA)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=100_000)

        assert compactor.check_and_compact(cid, "stub-model", ADA, service=given) is True
        assert (own.invoke_count, given.invoke_count) == (0, 1)

    def test_a_locked_compaction_model_gets_a_service_of_its_own(self, sync_db: Any) -> None:
        runtime = _runtime(sync_db, {"alpha": _answer("alpha"), "beta": _answer("beta")})
        try:
            mgr = _manager(sync_db, runtime, compaction_model=ALPHA)
            compacted: list[Any] = []
            assert mgr._compactor is not None

            def check_and_compact(*args: Any, service: Any = None, **kwargs: Any) -> bool:
                compacted.append(service)
                return False

            mgr._compactor.check_and_compact = check_and_compact  # type: ignore[method-assign]
            cid = _conversation(sync_db, ADA, BETA)
            mgr.send_message(cid, "hello", ADA, tools=[], roles=STAFF)
        finally:
            runtime.close()
        (service,) = compacted
        assert service.current_model == ALPHA
        assert (service.attribution.user_guid, service.attribution.source) == (ADA, "compaction")
