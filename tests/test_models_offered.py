"""The models offered limit what is chosen as well as what is listed, below the web (M5, M9).

A sub-agent may use only a model its conversation's connection offers (so it never
escapes the assignment check its conversation passed), while the conversation's
own pinned model keeps working; the action creation assistant may create an action
only on an offered model; and creating a conversation changes nothing shared.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark.core.conversation_manager import ConversationManager
from spark.scheduler.creation_tools import execute_creation_tool
from tests.model_helpers import FakeModels
from tests.test_conversation_manager import StubLLMService
from tests.test_conversation_manager_users import ADA, AGENTS_ON, RecordingAgentExecutor

OFFERED = [
    {"id": "test/offered-model", "model_id": "offered-model", "name": "offered-model"},
    {"id": "test/stub-model", "model_id": "stub-model", "name": "stub-model"},
]


class TestCreationAssistant:
    def test_an_action_may_use_only_an_offered_model(self, sync_db: Any) -> None:
        tool = {
            "name": "nightly",
            "action_prompt": "p",
            "model_id": "other-model",
            "schedule_type": "recurring",
            "schedule_value": "0 9 * * *",
        }
        config = {"_user_guid": ADA, "_offered_models": ["test/stub-model"]}
        refused = execute_creation_tool("create_autonomous_action", tool, config, sync_db)
        assert refused.startswith("Error") and "other-model" in refused
        made = execute_creation_tool(
            "create_autonomous_action", {**tool, "model_id": "test/stub-model"}, config, sync_db
        )
        assert "created successfully" in made


@pytest.fixture
def executors(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    import spark.core.agent_executor as agent_executor

    RecordingAgentExecutor.users = []
    monkeypatch.setattr(agent_executor, "AgentExecutor", RecordingAgentExecutor)
    return RecordingAgentExecutor.users


def _manager(sync_db: Any, models: list[dict[str, Any]]) -> tuple[ConversationManager, FakeModels]:
    source = FakeModels(StubLLMService(), models=models)
    mgr = ConversationManager(sync_db, source, embedded_tools_config=AGENTS_ON)
    return mgr, source


class TestSubAgents:
    def test_a_model_not_offered_is_refused(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        mgr, _ = _manager(sync_db, OFFERED[:1])
        cid = mgr.create_conversation("Ada's", "test/offered-model", ADA)
        text, is_error = mgr._execute_agent_spawn({"task": "t", "model_id": "stub-model"}, cid, ADA)
        assert is_error and "stub-model" in text
        assert executors == []

    def test_another_connections_model_is_refused(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        mgr, _ = _manager(sync_db, OFFERED)
        cid = mgr.create_conversation("Ada's", "test/offered-model", ADA)
        text, is_error = mgr._execute_agent_spawn(
            {"task": "t", "model_id": "elsewhere/offered-model"}, cid, ADA
        )
        assert is_error and "elsewhere/offered-model" in text
        assert executors == []

    def test_an_offered_model_on_the_same_connection_spawns(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        mgr, source = _manager(sync_db, OFFERED)
        cid = mgr.create_conversation("Ada's", "test/offered-model", ADA)
        text, is_error = mgr._execute_agent_spawn({"task": "t", "model_id": "stub-model"}, cid, ADA)
        assert not is_error, text
        assert executors == [(ADA, "t")]
        assert source.requested[-1] == "test/stub-model"

    def test_the_conversations_pinned_model_keeps_working(
        self, sync_db: Any, executors: list[tuple[str, str]]
    ) -> None:
        mgr, _ = _manager(sync_db, OFFERED[:1])
        cid = mgr.create_conversation("Ada's", "test/stub-model", ADA)
        text, is_error = mgr._execute_agent_spawn({"task": "t"}, cid, ADA)
        assert not is_error, text
        assert executors == [(ADA, "t")]

    def test_only_offered_models_are_listed_to_the_model(self, sync_db: Any) -> None:
        mgr, _ = _manager(sync_db, OFFERED[:1])
        text, is_error = mgr._list_provider_models("test/offered-model")
        assert not is_error
        assert "offered-model" in text and "stub-model" not in text


class TestCreatingAConversation:
    def test_creating_changes_nothing_shared(self, sync_db: Any) -> None:
        mgr, source = _manager(sync_db, OFFERED)
        mgr.create_conversation("Ada's", "test/stub-model", ADA)
        # No service was made: a turn takes its own when it runs.
        assert source.requested == []
