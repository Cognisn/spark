"""Tests for the ConversationManager and ContextCompactor."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from spark.core.context_compaction import (
    ContextCompactor,
    _find_in_flight_tool_messages,
    _format_messages,
)
from spark.core.conversation_manager import ConversationManager, _tool_result
from spark.database import Database
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection
from spark.llm.context_limits import ContextLimitResolver
from spark.llm.manager import LLMManager

USER = "test-user"


# -- Fixtures -----------------------------------------------------------------


class StubLLMService:
    """Stub LLM service for testing."""

    def __init__(self) -> None:
        self._model_id: str | None = None
        self.invoke_count = 0
        self.responses: list[dict] = []
        self._default_response = {
            "content": "Test response",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": "Test response"}],
        }

    def get_provider_name(self) -> str:
        return "Stub"

    def get_access_info(self) -> str:
        return "Stub"

    def list_available_models(self) -> list[dict]:
        return [
            {
                "id": "stub-model",
                "name": "Stub",
                "provider": "Stub",
                "supports_tools": True,
                "context_length": 200000,
            }
        ]

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id

    def supports_streaming(self) -> bool:
        return False

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        self.invoke_count += 1
        if self.responses:
            return self.responses.pop(0)
        return dict(self._default_response)

    def get_rate_limits(self) -> dict | None:
        return None

    @property
    def current_model(self) -> str | None:
        return self._model_id


@pytest.fixture()
def db(tmp_path) -> Database:
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    return Database(conn)


@pytest.fixture()
def stub_llm() -> StubLLMService:
    return StubLLMService()


@pytest.fixture()
def llm_manager(stub_llm: StubLLMService) -> LLMManager:
    mgr = LLMManager()
    mgr.register_provider(stub_llm)  # type: ignore[arg-type]
    return mgr


@pytest.fixture()
def manager(db: Database, llm_manager: LLMManager) -> ConversationManager:
    return ConversationManager(
        db.connection,
        llm_manager,
        ContextLimitResolver(),
        max_tool_iterations=5,
    )


# -- ConversationManager tests -----------------------------------------------


class TestConversationLifecycle:
    def test_create_conversation(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        assert cid > 0

    def test_get_conversations(self, manager: ConversationManager) -> None:
        manager.create_conversation("Chat 1", "stub-model", USER)
        manager.create_conversation("Chat 2", "stub-model", USER)
        convs = manager.get_conversations(USER)
        assert len(convs) == 2

    def test_get_conversation(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        conv = manager.get_conversation(cid, USER)
        assert conv is not None
        assert conv["name"] == "Test"

    def test_delete_conversation(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        manager.delete_conversation(cid, USER)
        convs = manager.get_conversations(USER)
        assert len(convs) == 0

    def test_get_messages_empty(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        msgs = manager.get_messages(cid)
        assert len(msgs) == 0


class TestSendMessage:
    def test_basic_send(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        result = manager.send_message(cid, "Hello!", USER)
        assert result["content"] == "Test response"
        assert result["iterations"] == 1
        assert result["usage"]["input_tokens"] == 50

    def test_messages_stored(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        manager.send_message(cid, "Hello!", USER)
        msgs = manager.get_messages(cid)
        # user message + assistant response
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_tool_use_loop(
        self, db: Database, llm_manager: LLMManager, stub_llm: StubLLMService
    ) -> None:
        mgr = ConversationManager(
            db.connection,
            llm_manager,
            ContextLimitResolver(),
            max_tool_iterations=10,
        )
        cid = mgr.create_conversation("Test", "stub-model", USER)

        # First response: tool_use, second: final text
        stub_llm.responses = [
            {
                "content": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 30, "output_tokens": 10},
                "tool_use": [{"type": "tool_use", "id": "t1", "name": "test_tool", "input": {}}],
                "content_blocks": [
                    {"type": "tool_use", "id": "t1", "name": "test_tool", "input": {}},
                ],
            },
            {
                "content": "Done with tools",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 40, "output_tokens": 15},
                "tool_use": None,
                "content_blocks": [{"type": "text", "text": "Done with tools"}],
            },
        ]

        result = mgr.send_message(cid, "Use a tool", USER)
        assert result["content"] == "Done with tools"
        assert result["iterations"] == 2
        assert len(result["tool_calls"]) == 1
        assert stub_llm.invoke_count == 2

    def test_tool_permission_denied(
        self, db: Database, llm_manager: LLMManager, stub_llm: StubLLMService
    ) -> None:
        def deny_all(name: str, inp: dict) -> str:
            return "denied"

        mgr = ConversationManager(
            db.connection,
            llm_manager,
            ContextLimitResolver(),
            tool_permission_callback=deny_all,
        )
        cid = mgr.create_conversation("Test", "stub-model", USER)

        stub_llm.responses = [
            {
                "content": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 30, "output_tokens": 10},
                "tool_use": [{"type": "tool_use", "id": "t1", "name": "blocked_tool", "input": {}}],
                "content_blocks": [
                    {"type": "tool_use", "id": "t1", "name": "blocked_tool", "input": {}}
                ],
            },
            {
                "content": "OK, tool was denied",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 40, "output_tokens": 15},
                "tool_use": None,
                "content_blocks": [{"type": "text", "text": "OK, tool was denied"}],
            },
        ]

        result = mgr.send_message(cid, "Try tool", USER)
        assert result["content"] == "OK, tool was denied"

    def test_conversation_not_found(self, manager: ConversationManager) -> None:
        with pytest.raises(ValueError, match="not found"):
            manager.send_message(99999, "Hello", USER)

    def test_max_iterations(
        self, db: Database, llm_manager: LLMManager, stub_llm: StubLLMService
    ) -> None:
        mgr = ConversationManager(
            db.connection,
            llm_manager,
            ContextLimitResolver(),
            max_tool_iterations=2,
        )
        cid = mgr.create_conversation("Test", "stub-model", USER)

        # Always return tool_use — should stop at max iterations
        tool_response = {
            "content": "",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "tool_use": [{"type": "tool_use", "id": "t1", "name": "loop_tool", "input": {}}],
            "content_blocks": [{"type": "tool_use", "id": "t1", "name": "loop_tool", "input": {}}],
        }
        stub_llm.responses = [tool_response, tool_response, tool_response]

        result = mgr.send_message(cid, "Loop", USER)
        assert result["iterations"] == 2

    def test_status_callback(
        self, db: Database, llm_manager: LLMManager, stub_llm: StubLLMService
    ) -> None:
        events: list[tuple] = []

        def callback(event: str, data: dict) -> None:
            events.append((event, data))

        mgr = ConversationManager(
            db.connection,
            llm_manager,
            ContextLimitResolver(),
        )
        cid = mgr.create_conversation("Test", "stub-model", USER)

        stub_llm.responses = [
            {
                "content": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "tool_use": [
                    {"type": "tool_use", "id": "t1", "name": "my_tool", "input": {"x": 1}}
                ],
                "content_blocks": [
                    {"type": "tool_use", "id": "t1", "name": "my_tool", "input": {"x": 1}}
                ],
            },
            {
                "content": "Final",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "tool_use": None,
                "content_blocks": [{"type": "text", "text": "Final"}],
            },
        ]

        mgr.send_message(cid, "Go", USER, status_callback=callback)
        event_types = [e[0] for e in events]
        assert "tool_call" in event_types
        assert "tool_result" in event_types
        assert "tool_iteration_complete" in event_types


class TestSystemInstructions:
    def test_includes_identity(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation("Test", "stub-model", USER)
        conv = manager.get_conversation(cid, USER)
        system = manager._build_system_instructions(conv)
        assert "Spark" in system
        assert "Current date/time" in system

    def test_includes_global_instructions(self, db: Database, llm_manager: LLMManager) -> None:
        mgr = ConversationManager(
            db.connection,
            llm_manager,
            ContextLimitResolver(),
            global_instructions="Always be concise.",
        )
        cid = mgr.create_conversation("Test", "stub-model", USER)
        conv = mgr.get_conversation(cid, USER)
        system = mgr._build_system_instructions(conv)
        assert "Always be concise." in system

    def test_includes_conversation_instructions(self, manager: ConversationManager) -> None:
        cid = manager.create_conversation(
            "Test",
            "stub-model",
            USER,
            instructions="You are a Python expert.",
        )
        conv = manager.get_conversation(cid, USER)
        system = manager._build_system_instructions(conv)
        assert "Python expert" in system


# -- ContextCompactor tests ---------------------------------------------------


class TestFormatMessages:
    def test_simple(self) -> None:
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = _format_messages(msgs)
        assert "[USER]: Hello" in result
        assert "[ASSISTANT]: Hi there" in result

    def test_truncates_long(self) -> None:
        msgs = [{"role": "user", "content": "x" * 5000}]
        result = _format_messages(msgs)
        assert "[truncated]" in result

    def test_content_blocks(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Calling tool"},
                    {"type": "tool_use", "name": "read_file", "id": "t1", "input": {}},
                ],
            }
        ]
        result = _format_messages(msgs)
        assert "Calling tool" in result
        assert "read_file" in result


class TestFindInFlightToolMessages:
    def test_no_tools(self) -> None:
        msgs = [{"id": 1, "content": "hello"}]
        assert _find_in_flight_tool_messages(msgs) == set()

    def test_completed_tools(self) -> None:
        msgs = [
            {"id": 1, "content": [{"type": "tool_use", "id": "t1", "name": "a"}]},
            {"id": 2, "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        ]
        assert _find_in_flight_tool_messages(msgs) == set()

    def test_in_flight_tool(self) -> None:
        msgs = [
            {"id": 1, "content": [{"type": "tool_use", "id": "t1", "name": "a"}]},
            # No matching tool_result
        ]
        assert _find_in_flight_tool_messages(msgs) == {1}

    def test_mixed(self) -> None:
        msgs = [
            {"id": 1, "content": [{"type": "tool_use", "id": "t1", "name": "a"}]},
            {"id": 2, "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            {"id": 3, "content": [{"type": "tool_use", "id": "t2", "name": "b"}]},
        ]
        assert _find_in_flight_tool_messages(msgs) == {3}


class TestContextCompactor:
    def test_no_compaction_below_threshold(self, db: Database, stub_llm: StubLLMService) -> None:
        from spark.database import conversations, messages

        compactor = ContextCompactor(
            stub_llm,
            db.connection,
            ContextLimitResolver(),  # type: ignore[arg-type]
            threshold=0.7,
        )
        cid = conversations.create_conversation(db.connection, "Test", "stub-model", USER)
        messages.add_message(db.connection, cid, "user", "short msg", 10, USER)

        result = compactor.check_and_compact(cid, "stub-model", USER)
        assert result is False

    def test_deferred_during_tool_use(self, db: Database, stub_llm: StubLLMService) -> None:
        from spark.database import conversations

        compactor = ContextCompactor(
            stub_llm,
            db.connection,
            ContextLimitResolver(),  # type: ignore[arg-type]
            threshold=0.01,  # very low threshold to trigger
        )
        cid = conversations.create_conversation(db.connection, "Test", "stub-model", USER)
        # Manually set high token count
        conversations.update_conversation(db.connection, cid, USER, total_tokens=5000)

        result = compactor.check_and_compact(cid, "stub-model", USER, in_tool_use_loop=True)
        assert result is False  # Deferred

    def test_skips_when_conversation_not_found(
        self, db: Database, stub_llm: StubLLMService
    ) -> None:
        """Wrong user_guid must not silently succeed — regression for empty user_guid bug."""
        from spark.database import conversations

        compactor = ContextCompactor(
            stub_llm,
            db.connection,
            ContextLimitResolver(),  # type: ignore[arg-type]
            threshold=0.01,
        )
        cid = conversations.create_conversation(db.connection, "Test", "stub-model", USER)
        conversations.update_conversation(db.connection, cid, USER, total_tokens=100_000)

        # With a real user_guid, compaction runs (stub LLM handles it).
        # With an empty user_guid, the conversation is not found and we skip.
        assert compactor.check_and_compact(cid, "stub-model", "") is False


class TestToolResult:
    def test_basic(self) -> None:
        result = _tool_result("t1", "ok")
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "t1"
        assert result["content"] == "ok"
        assert "is_error" not in result

    def test_error(self) -> None:
        result = _tool_result("t1", "failed", is_error=True)
        assert result["is_error"] is True
