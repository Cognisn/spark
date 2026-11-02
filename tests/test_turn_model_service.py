"""Each chat turn answers through a model service of its own.

One LLM manager, and one conversation manager, serve every user of a server. A
turn used to set its conversation's model on the shared manager and then invoke
the manager's active provider, so two turns on different models at once could
answer with each other's model. Each turn now resolves a service for its
conversation's model (:meth:`spark.llm.manager.LLMManager.service_for`) and uses
it for every call the turn makes: invoking, counting tokens, the tool loop,
prompt caching and compaction. The shared manager's provider and model never move.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.core.context_compaction import ContextCompactor
from spark.core.conversation_manager import ConversationManager
from spark.database import conversations
from spark.llm.context_limits import ContextLimitResolver
from spark.llm.manager import LLMManager
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.test_conversation_manager import StubLLMService

ADA = "user-ada-example"
GRACE = "user-grace-example"


def _text(text: str) -> dict[str, Any]:
    return {
        "content": text,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "tool_use": None,
        "content_blocks": [{"type": "text", "text": text}],
    }


def _tool_call(name: str) -> dict[str, Any]:
    call = {"type": "tool_use", "id": "call-1", "name": name, "input": {}}
    return {
        "content": "",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "tool_use": [call],
        "content_blocks": [call],
    }


class RecordingProvider(StubLLMService):
    """A provider listing ``models`` that records, in a list its copies share, the
    model every call was made with. ``barrier`` holds each invocation until the
    other turn's has started too, so the turns interleave."""

    def __init__(
        self,
        name: str,
        models: tuple[str, ...],
        calls: list[tuple[str, str | None, Any]],
        *,
        barrier: threading.Barrier | None = None,
        script: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self.models = models
        self.calls = calls
        self.barrier = barrier
        self.script = script if script is not None else []
        self.lock = threading.Lock()

    def get_provider_name(self) -> str:
        return self.name

    def list_available_models(self) -> list[dict]:
        return [
            {"id": m, "name": m, "provider": self.name, "supports_tools": True, "context_length": 8}
            for m in self.models
        ]

    def count_tokens(self, text: str) -> int:
        with self.lock:
            self.calls.append(("count", self._model_id, None))
        return len(text) // 4

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        if self.barrier is not None:
            self.barrier.wait()
        model = self._model_id
        with self.lock:
            self.calls.append(("invoke", model, (messages[0]["content"], kwargs)))
            if self.script:
                return self.script.pop(0)
        return _text(f"answer from {model}")


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


def _manager(sync_db: Any, llm: LLMManager, **kwargs: Any) -> ConversationManager:
    return ConversationManager(
        sync_db, llm, ContextLimitResolver(), user_guid="process-guid", **kwargs
    )


class TestConcurrentTurns:
    def test_two_users_on_different_models_each_get_their_own(
        self, sync_db: Any, either_mode: str
    ) -> None:
        calls: list[tuple[str, str | None, Any]] = []
        barrier = threading.Barrier(2, timeout=30)
        provider = RecordingProvider("Shared", ("model-a", "model-b"), calls, barrier=barrier)
        llm = LLMManager()
        llm.register_provider(provider)  # type: ignore[arg-type]
        mgr = _manager(sync_db, llm)
        chosen = {ADA: "model-a", GRACE: "model-b"}
        cids = {user: _conversation(sync_db, user, model) for user, model in chosen.items()}
        answers: dict[str, Any] = {}

        def turn(user: str) -> None:
            answers[user] = mgr.send_message(cids[user], f"hello from {user}", user, tools=[])

        threads = [threading.Thread(target=turn, args=(user,)) for user in chosen]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        for user, model in chosen.items():
            assert answers[user]["content"] == f"answer from {model}"
        invoked = {(c[1], c[2][0]) for c in calls if c[0] == "invoke"}
        assert invoked == {("model-a", f"hello from {ADA}"), ("model-b", f"hello from {GRACE}")}
        # The shared provider was never given either model.
        assert provider.current_model is None


class TestOneTurn:
    def _two_providers(
        self, script: list[dict[str, Any]] | None = None
    ) -> tuple[LLMManager, RecordingProvider, RecordingProvider, list[Any]]:
        calls: list[tuple[str, str | None, Any]] = []
        first = RecordingProvider("First", ("model-a",), calls)
        second = RecordingProvider("Second", ("model-b",), calls, script=script)
        llm = LLMManager()
        llm.register_provider(first)  # type: ignore[arg-type]
        llm.register_provider(second)  # type: ignore[arg-type]
        return llm, first, second, calls

    def test_leaves_the_shared_manager_unchanged(self, sync_db: Any, either_mode: str) -> None:
        llm, first, second, calls = self._two_providers()
        mgr = _manager(sync_db, llm)
        cid = _conversation(sync_db, ADA, "model-b")

        result = mgr.send_message(cid, "hello", ADA, tools=[])

        assert result["content"] == "answer from model-b"
        assert llm.active_provider == "First"
        assert (first.current_model, second.current_model) == (None, None)

    def test_every_call_of_the_turn_uses_its_model(self, sync_db: Any, either_mode: str) -> None:
        llm, first, second, calls = self._two_providers(
            script=[_tool_call("no_such_tool"), _text("done")]
        )
        mgr = _manager(sync_db, llm)
        compacted: list[Any] = []
        assert mgr._compactor is not None

        def check_and_compact(*args: Any, service: Any = None, **kwargs: Any) -> bool:
            compacted.append(service)
            return False

        mgr._compactor.check_and_compact = check_and_compact  # type: ignore[method-assign]
        cid = _conversation(sync_db, ADA, "model-b")

        result = mgr.send_message(cid, "hello", ADA, tools=[])

        assert result["content"] == "done"
        assert result["iterations"] == 2
        invoked = [c for c in calls if c[0] == "invoke"]
        counted = [c for c in calls if c[0] == "count"]
        assert [c[1] for c in invoked] == ["model-b", "model-b"]
        assert counted and {c[1] for c in counted} == {"model-b"}
        assert all(c[2][1]["prompt_caching"] is True for c in invoked)
        assert [getattr(s, "current_model", None) for s in compacted] == ["model-b"]
        assert compacted[0] is not second

    def test_a_pinned_model_no_longer_offered_still_answers(
        self, sync_db: Any, either_mode: str
    ) -> None:
        llm, first, second, calls = self._two_providers()
        llm.set_offered(lambda: ["model-a"])
        mgr = _manager(sync_db, llm)
        cid = _conversation(sync_db, ADA, "model-b")

        assert mgr.send_message(cid, "hello", ADA, tools=[])["content"] == "answer from model-b"

    def test_a_model_no_provider_lists_falls_back_to_the_active_service(
        self, sync_db: Any, either_mode: str
    ) -> None:
        llm, first, second, calls = self._two_providers()
        first.set_model("model-a")
        mgr = _manager(sync_db, llm)
        cid = _conversation(sync_db, ADA, "model-gone")

        assert mgr.send_message(cid, "hello", ADA, tools=[])["content"] == "answer from model-a"
        assert (llm.active_provider, first.current_model) == ("First", "model-a")

    def test_the_turns_provider_lists_the_models_for_sub_agents(self, sync_db: Any) -> None:
        llm, first, second, calls = self._two_providers()
        mgr = _manager(sync_db, llm)

        text, is_error = mgr._list_provider_models("model-b")

        assert not is_error
        assert "Second" in text and "model-b" in text and "model-a" not in text


class TestServiceFor:
    def test_a_copy_with_its_own_model(self) -> None:
        calls: list[Any] = []
        provider = RecordingProvider("Shared", ("model-a", "model-b"), calls)
        llm = LLMManager()
        llm.register_provider(provider)  # type: ignore[arg-type]

        a = llm.service_for("model-a")
        b = llm.service_for("model-b")

        assert a is not None and b is not None
        assert (a.current_model, b.current_model) == ("model-a", "model-b")
        assert a is not provider and b is not provider
        assert provider.current_model is None
        assert llm.service_for("model-gone") is None

    def test_does_not_ask_whether_the_model_is_offered(self) -> None:
        provider = RecordingProvider("Shared", ("model-a", "model-b"), [])
        llm = LLMManager()
        llm.register_provider(provider)  # type: ignore[arg-type]
        llm.set_offered(lambda: ["model-a"])

        service = llm.service_for("model-b")

        assert service is not None and service.current_model == "model-b"

    def test_a_provider_that_cannot_be_cloned_is_copied_never_changed(self, sync_db: Any) -> None:
        provider = RecordingProvider("Unclonable", ("model-a", "model-b"), [])
        llm = LLMManager()
        llm.register_provider(provider)  # type: ignore[arg-type]
        mgr = _manager(sync_db, llm)

        service = mgr._get_llm_service_for_model("model-b")

        assert service is not None and service.current_model == "model-b"
        assert provider.current_model is None


class TestCompactorService:
    def test_compacts_with_the_service_it_is_given(self, sync_db: Any) -> None:
        from spark.database import messages

        own = StubLLMService()
        given = StubLLMService()
        given.responses = [_text("Summary of the prior conversation: " + ("blah " * 60))]
        compactor = ContextCompactor(
            own, sync_db, ContextLimitResolver(), threshold=0.01  # type: ignore[arg-type]
        )
        cid = sync_db.call(conversations.create_conversation, "Test", "stub-model", ADA)
        for i in range(4):
            sync_db.call(messages.add_message, cid, "user", f"message {i}", 50, ADA)
        sync_db.call(conversations.update_conversation, cid, ADA, total_tokens=100_000)

        assert compactor.check_and_compact(cid, "stub-model", ADA, service=given) is True
        assert (own.invoke_count, given.invoke_count) == (0, 1)

    def test_a_locked_compaction_model_gets_a_service_of_its_own(self, sync_db: Any) -> None:
        llm, first, second, calls = TestOneTurn()._two_providers()
        mgr = _manager(sync_db, llm, compaction_model="model-a")
        compacted: list[Any] = []
        assert mgr._compactor is not None

        def check_and_compact(*args: Any, service: Any = None, **kwargs: Any) -> bool:
            compacted.append(service)
            return False

        mgr._compactor.check_and_compact = check_and_compact  # type: ignore[method-assign]
        cid = _conversation(sync_db, ADA, "model-b")

        mgr.send_message(cid, "hello", ADA, tools=[])

        assert [getattr(s, "current_model", None) for s in compacted] == ["model-a"]
        assert first.current_model is None
