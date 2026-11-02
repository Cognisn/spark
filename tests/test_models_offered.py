"""The models offered limit what is chosen as well as what is listed (D38), below the web.

The LLM manager answers whether a model is offered by the list's own rule; a
model chosen for a sub-agent or by the action creation assistant must be offered,
while a conversation's own pinned model keeps working; and a server never sets
the shared manager's model when a user creates a conversation.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.llm.manager import LLMManager
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.scheduler.creation_tools import execute_creation_tool
from tests.test_conversation_manager import StubLLMService
from tests.test_conversation_manager_users import ADA, RecordingAgentExecutor, _manager


class TwoModels(StubLLMService):
    def list_available_models(self) -> list[dict]:
        return [
            {"id": m, "name": m, "provider": "Stub", "supports_tools": True, "context_length": 8}
            for m in ("stub-model", "other-model")
        ]


def _llm(offered: list[str]) -> LLMManager:
    llm = LLMManager()
    llm.register_provider(TwoModels())  # type: ignore[arg-type]
    llm.set_offered(lambda: offered)
    return llm


class TestManager:
    def test_an_empty_list_offers_every_model(self) -> None:
        llm = _llm([])
        assert llm.offered_models() == frozenset()
        assert llm.is_offered("other-model")

    def test_without_a_list_every_model_is_offered(self) -> None:
        llm = LLMManager()
        assert llm.is_offered("anything")

    def test_the_list_limits_choosing_as_it_limits_listing(self) -> None:
        llm = _llm(["stub-model"])
        assert [m["id"] for m in llm.list_all_models()] == ["stub-model"]
        assert llm.is_offered("stub-model")
        assert not llm.is_offered("other-model")

    def test_set_model_still_accepts_a_model_that_left_the_list(self) -> None:
        llm = _llm(["stub-model"])
        llm.set_model("other-model")
        assert llm.active_service.current_model == "other-model"  # type: ignore[union-attr]


class TestCreationAssistant:
    def test_an_action_may_use_only_an_offered_model(self, sync_db: Any) -> None:
        tool = {
            "name": "nightly",
            "action_prompt": "p",
            "model_id": "other-model",
            "schedule_type": "recurring",
            "schedule_value": "0 9 * * *",
        }
        config = {"_user_guid": ADA, "_offered_models": ["stub-model"]}
        refused = execute_creation_tool("create_autonomous_action", tool, config, sync_db)
        assert refused.startswith("Error") and "other-model" in refused
        made = execute_creation_tool(
            "create_autonomous_action", {**tool, "model_id": "stub-model"}, config, sync_db
        )
        assert "created successfully" in made


@pytest.fixture
def server() -> Iterator[None]:
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y"))
    )
    yield
    mode.reset()


@pytest.fixture
def executors(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import spark.core.agent_executor as agent_executor

    RecordingAgentExecutor.users = []
    monkeypatch.setattr(agent_executor, "AgentExecutor", RecordingAgentExecutor)
    return RecordingAgentExecutor.users


def _offering(sync_db: Any, offered: list[str]) -> Any:
    mgr = _manager(sync_db, None)
    mgr._llm.set_offered(lambda: offered)
    return mgr


class TestSubAgents:
    def test_a_model_not_offered_is_refused(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        mgr = _offering(sync_db, ["offered-model"])
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        text, is_error = mgr._execute_agent_spawn({"task": "t", "model_id": "stub-model"}, cid, ADA)
        assert is_error and "stub-model" in text
        assert executors == []

    def test_the_conversations_pinned_model_keeps_working(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        mgr = _offering(sync_db, ["offered-model"])
        cid = mgr.create_conversation("Ada's", "stub-model", ADA)
        text, is_error = mgr._execute_agent_spawn({"task": "t"}, cid, ADA)
        assert not is_error, text
        assert executors == [(ADA, "t")]

    def test_only_offered_models_are_listed_to_the_model(self, sync_db: Any) -> None:
        mgr = _offering(sync_db, ["offered-model"])
        text, is_error = mgr._list_provider_models()
        assert "stub-model" not in text


class TestCreatingAConversation:
    def test_a_server_never_sets_the_shared_model(self, sync_db: Any, server: None) -> None:
        mgr = _manager(sync_db, None)
        mgr.create_conversation("Ada's", "stub-model", ADA)
        assert mgr._llm.active_service.current_model is None  # type: ignore[attr-defined]

    def test_personal_mode_sets_it_as_before(self, sync_db: Any) -> None:
        mode.reset()
        mgr = _manager(sync_db, None)
        mgr.create_conversation("Ada's", "stub-model", ADA)
        assert mgr._llm.active_service.current_model == "stub-model"  # type: ignore[attr-defined]
