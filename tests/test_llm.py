"""Tests for the LLM provider abstraction layer."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from spark.llm.base import LLMService
from spark.llm.context_limits import ContextLimitResolver
from spark.llm.manager import LLMManager


# -- Concrete stub for testing ------------------------------------------------


class StubProvider(LLMService):
    """Minimal LLMService implementation for testing."""

    def __init__(self, name: str = "Stub", models: list[dict] | None = None) -> None:
        self._name = name
        self._models = models or [
            {"id": "stub-1", "name": "Stub Model 1", "provider": name, "supports_tools": True, "context_length": 8192},
            {"id": "stub-2", "name": "Stub Model 2", "provider": name, "supports_tools": False, "context_length": 4096},
        ]
        self._model_id: str | None = None
        self._invoke_response: dict[str, Any] = {
            "content": "Hello from stub",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": "Hello from stub"}],
        }

    def get_provider_name(self) -> str:
        return self._name

    def get_access_info(self) -> str:
        return f"{self._name} (test)"

    def list_available_models(self) -> list[dict[str, Any]]:
        return self._models

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id

    def supports_streaming(self) -> bool:
        return False

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict[str, Any]:
        return self._invoke_response


# -- LLMManager Tests ---------------------------------------------------------


class TestLLMManager:
    def test_register_provider(self) -> None:
        mgr = LLMManager()
        provider = StubProvider("TestProvider")
        mgr.register_provider(provider)
        assert "TestProvider" in mgr.providers
        assert mgr.active_provider == "TestProvider"

    def test_first_registered_becomes_active(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(StubProvider("First"))
        mgr.register_provider(StubProvider("Second"))
        assert mgr.active_provider == "First"

    def test_set_model_by_provider(self) -> None:
        mgr = LLMManager()
        p = StubProvider("P1")
        mgr.register_provider(p)
        mgr.set_model("stub-1", provider_name="P1")
        assert p._model_id == "stub-1"
        assert mgr.active_provider == "P1"

    def test_set_model_auto_discover(self) -> None:
        mgr = LLMManager()
        p1 = StubProvider("P1", [{"id": "model-a", "name": "A", "provider": "P1", "supports_tools": True, "context_length": 8192}])
        p2 = StubProvider("P2", [{"id": "model-b", "name": "B", "provider": "P2", "supports_tools": True, "context_length": 8192}])
        mgr.register_provider(p1)
        mgr.register_provider(p2)
        mgr.set_model("model-b")
        assert mgr.active_provider == "P2"

    def test_set_model_not_found(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(StubProvider())
        with pytest.raises(ValueError, match="not found"):
            mgr.set_model("nonexistent")

    def test_set_model_unknown_provider(self) -> None:
        mgr = LLMManager()
        with pytest.raises(ValueError, match="Unknown provider"):
            mgr.set_model("x", provider_name="NoSuch")

    def test_list_all_models(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(StubProvider("A", [{"id": "a1", "name": "A1", "provider": "A", "supports_tools": True, "context_length": 8192}]))
        mgr.register_provider(StubProvider("B", [{"id": "b1", "name": "B1", "provider": "B", "supports_tools": True, "context_length": 8192}]))
        models = mgr.list_all_models()
        assert len(models) == 2
        ids = {m["id"] for m in models}
        assert ids == {"a1", "b1"}

    def test_invoke_model(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(StubProvider())
        result = mgr.invoke_model([{"role": "user", "content": "hi"}])
        assert result["content"] == "Hello from stub"
        assert result["stop_reason"] == "end_turn"

    def test_invoke_no_provider(self) -> None:
        mgr = LLMManager()
        with pytest.raises(RuntimeError, match="No active"):
            mgr.invoke_model([{"role": "user", "content": "hi"}])

    def test_count_tokens(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(StubProvider())
        count = mgr.count_tokens("hello world test text")
        assert isinstance(count, int)
        assert count > 0

    def test_count_tokens_no_provider(self) -> None:
        mgr = LLMManager()
        assert mgr.count_tokens("some text here test") > 0

    def test_supports_streaming(self) -> None:
        mgr = LLMManager()
        mgr.register_provider(StubProvider())
        assert mgr.supports_streaming() is False

    def test_supports_streaming_no_provider(self) -> None:
        mgr = LLMManager()
        assert mgr.supports_streaming() is False

    def test_active_service(self) -> None:
        mgr = LLMManager()
        p = StubProvider()
        mgr.register_provider(p)
        assert mgr.active_service is p

    def test_active_service_none(self) -> None:
        mgr = LLMManager()
        assert mgr.active_service is None


# -- ContextLimitResolver Tests -----------------------------------------------


class TestContextLimitResolver:
    def test_known_model(self) -> None:
        r = ContextLimitResolver()
        ctx, out = r.resolve("claude-3-5-sonnet-20241022")
        assert ctx == 200_000
        assert out == 8_192

    def test_partial_match(self) -> None:
        r = ContextLimitResolver()
        ctx = r.get_context_window("some-claude-opus-4-variant")
        assert ctx == 200_000

    def test_ollama_model(self) -> None:
        r = ContextLimitResolver()
        ctx = r.get_context_window("llama3.1:70b")
        assert ctx == 128_000

    def test_gemini_model(self) -> None:
        r = ContextLimitResolver()
        ctx = r.get_context_window("gemini-2.5-pro-preview-06-05")
        assert ctx == 1_000_000

    def test_unknown_model_default(self) -> None:
        r = ContextLimitResolver()
        ctx, out = r.resolve("totally-unknown-model")
        assert ctx == 8_192
        assert out == 4_096

    def test_config_overrides_exact(self) -> None:
        r = ContextLimitResolver({"my-custom-model": {"context_window": 50_000, "max_output": 10_000}})
        ctx, out = r.resolve("my-custom-model")
        assert ctx == 50_000
        assert out == 10_000

    def test_config_overrides_partial(self) -> None:
        r = ContextLimitResolver({"custom": {"context_window": 32_000, "max_output": 8_000}})
        ctx, out = r.resolve("my-custom-v2")
        assert ctx == 32_000
        assert out == 8_000

    def test_compaction_threshold(self) -> None:
        r = ContextLimitResolver()
        threshold = r.get_compaction_threshold("claude-3-5-sonnet-20241022", 0.3)
        assert threshold == 60_000

    def test_emergency_threshold(self) -> None:
        r = ContextLimitResolver()
        threshold = r.get_emergency_threshold("claude-3-5-sonnet-20241022", 0.95)
        assert threshold == 190_000

    def test_get_max_output(self) -> None:
        r = ContextLimitResolver()
        assert r.get_max_output("grok-4-0709") == 16_384

    def test_xai_model(self) -> None:
        r = ContextLimitResolver()
        ctx = r.get_context_window("grok-4.1-fast-reasoning")
        assert ctx == 2_000_000


# -- Base class properties ----------------------------------------------------


class TestLLMServiceBase:
    def test_current_model_default(self) -> None:
        p = StubProvider()
        assert p.current_model is None

    def test_current_model_after_set(self) -> None:
        p = StubProvider()
        p.set_model("stub-1")
        assert p.current_model == "stub-1"

    def test_get_rate_limits_default(self) -> None:
        p = StubProvider()
        assert p.get_rate_limits() is None


# -- Anthropic message/tool helpers -------------------------------------------


class TestAnthropicHelpers:
    def test_clean_messages(self) -> None:
        from spark.llm.anthropic_direct import _clean_messages

        msgs = [{"role": "user", "content": "hi", "extra": "field"}]
        cleaned = _clean_messages(msgs)
        assert len(cleaned) == 1
        assert "extra" not in cleaned[0]

    def test_convert_tools(self) -> None:
        from spark.llm.anthropic_direct import _convert_tools

        tools = [{"name": "read_file", "description": "Read a file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
        converted = _convert_tools(tools)
        assert len(converted) == 1
        assert converted[0]["name"] == "read_file"
        assert "input_schema" in converted[0]

    def test_convert_tools_adds_type(self) -> None:
        from spark.llm.anthropic_direct import _convert_tools

        tools = [{"name": "t", "inputSchema": {"properties": {}}}]
        converted = _convert_tools(tools)
        assert converted[0]["input_schema"]["type"] == "object"

    def test_get_model_max_output(self) -> None:
        from spark.llm.anthropic_direct import _get_model_max_output

        assert _get_model_max_output("claude-opus-4-20250514") == 32_000
        assert _get_model_max_output("claude-3-haiku-20240307") == 4_096
        assert _get_model_max_output("unknown-model") == 4_096


# -- Bedrock message conversion -----------------------------------------------


class TestBedrockHelpers:
    def test_convert_messages_string(self) -> None:
        from spark.llm.bedrock import _convert_messages

        msgs = [{"role": "user", "content": "hello"}]
        converted = _convert_messages(msgs)
        assert converted[0]["content"] == [{"text": "hello"}]

    def test_convert_messages_blocks(self) -> None:
        from spark.llm.bedrock import _convert_messages

        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "Let me check"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "/tmp"}},
        ]}]
        converted = _convert_messages(msgs)
        blocks = converted[0]["content"]
        assert blocks[0] == {"text": "Let me check"}
        assert "toolUse" in blocks[1]

    def test_convert_tools(self) -> None:
        from spark.llm.bedrock import _convert_tools

        tools = [{"name": "search", "description": "Search", "inputSchema": {"type": "object"}}]
        converted = _convert_tools(tools)
        assert "toolSpec" in converted[0]
        assert converted[0]["toolSpec"]["name"] == "search"

    def test_map_stop_reason(self) -> None:
        from spark.llm.bedrock import _map_stop_reason

        assert _map_stop_reason("end_turn") == "end_turn"
        assert _map_stop_reason("tool_use") == "tool_use"
        assert _map_stop_reason("stop_sequence") == "end_turn"


# -- Ollama helpers -----------------------------------------------------------


class TestOllamaHelpers:
    def test_supports_tools(self) -> None:
        from spark.llm.ollama import _supports_tools

        assert _supports_tools("llama3:8b") is True
        assert _supports_tools("mistral:latest") is True
        assert _supports_tools("phi:latest") is False

    def test_estimate_context(self) -> None:
        from spark.llm.ollama import _estimate_context

        assert _estimate_context("llama3:70b") == 128_000
        assert _estimate_context("mistral:latest") == 32_768
        assert _estimate_context("unknown") == 8_192

    def test_convert_tools(self) -> None:
        from spark.llm.ollama import _convert_tools

        tools = [{"name": "t1", "description": "desc", "inputSchema": {"type": "object"}}]
        converted = _convert_tools(tools)
        assert converted[0]["type"] == "function"
        assert converted[0]["function"]["name"] == "t1"

    def test_convert_messages_with_system(self) -> None:
        from spark.llm.ollama import _convert_messages

        msgs = [{"role": "user", "content": "hi"}]
        converted = _convert_messages(msgs, system="Be helpful")
        assert converted[0]["role"] == "system"
        assert converted[0]["content"] == "Be helpful"
        assert converted[1]["content"] == "hi"


# -- xAI helpers --------------------------------------------------------------


class TestXAIHelpers:
    def test_convert_tools(self) -> None:
        from spark.llm.xai import _convert_tools

        tools = [{"name": "t1", "description": "desc", "inputSchema": {"type": "object"}}]
        converted = _convert_tools(tools)
        assert converted[0]["type"] == "function"
        assert converted[0]["function"]["name"] == "t1"

    def test_convert_messages_with_system(self) -> None:
        from spark.llm.xai import _convert_messages

        msgs = [{"role": "user", "content": "hi"}]
        converted = _convert_messages(msgs, system="Be helpful")
        assert converted[0]["role"] == "system"
        assert converted[1]["content"] == "hi"

    def test_convert_messages_tool_results(self) -> None:
        from spark.llm.xai import _convert_messages

        msgs = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
            ]},
        ]
        converted = _convert_messages(msgs)
        tool_result = [m for m in converted if m["role"] == "tool"]
        assert len(tool_result) == 1
        assert tool_result[0]["tool_call_id"] == "t1"


# -- Gemini helpers -----------------------------------------------------------


class TestGeminiHelpers:
    def test_clean_schema(self) -> None:
        from spark.llm.google_gemini import _clean_schema

        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "default": "foo"}},
            "additionalProperties": False,
            "$ref": "#/defs/X",
        }
        cleaned = _clean_schema(schema)
        assert "additionalProperties" not in cleaned
        assert "$ref" not in cleaned
        assert "default" not in cleaned["properties"]["name"]
        assert cleaned["properties"]["name"]["type"] == "string"

    def test_clean_schema_nested(self) -> None:
        from spark.llm.google_gemini import _clean_schema

        schema = {
            "type": "array",
            "items": {"type": "string", "examples": ["a", "b"]},
        }
        cleaned = _clean_schema(schema)
        assert "examples" not in cleaned["items"]
