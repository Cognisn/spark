"""Tests for the Bedrock provider's Converse request building and prompt caching."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spark.llm.bedrock import BedrockProvider

MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"


def _ok_response(text: str = "hi", **usage_extra) -> dict:
    usage = {"inputTokens": 5, "outputTokens": 3}
    usage.update(usage_extra)
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": usage,
        "stopReason": "end_turn",
    }


@pytest.fixture()
def provider() -> BedrockProvider:
    p = BedrockProvider(region="us-east-1")
    p._client = MagicMock()
    p._resolve_model_id = lambda m: m  # type: ignore[method-assign]
    p._model_id = MODEL
    return p


TOOLS = [{"name": "web_search", "description": "d", "inputSchema": {"type": "object"}}]


class TestBedrockPromptCaching:
    def test_cachepoints_added_when_enabled(self, provider: BedrockProvider) -> None:
        provider._client.converse.return_value = _ok_response()
        provider.invoke_model(
            [{"role": "user", "content": "hello"}],
            system="You are a debater.",
            tools=TOOLS,
            prompt_caching=True,
        )
        req = provider._client.converse.call_args.kwargs
        assert {"cachePoint": {"type": "default"}} in req["system"]
        assert req["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}

    def test_no_cachepoints_when_disabled(self, provider: BedrockProvider) -> None:
        provider._client.converse.return_value = _ok_response()
        provider.invoke_model(
            [{"role": "user", "content": "hello"}],
            system="You are a debater.",
            tools=TOOLS,
            prompt_caching=False,
        )
        req = provider._client.converse.call_args.kwargs
        assert all("cachePoint" not in b for b in req["system"])
        assert all("cachePoint" not in t for t in req["toolConfig"]["tools"])

    def test_default_is_no_caching(self, provider: BedrockProvider) -> None:
        provider._client.converse.return_value = _ok_response()
        provider.invoke_model([{"role": "user", "content": "hi"}], system="s")
        req = provider._client.converse.call_args.kwargs
        assert all("cachePoint" not in b for b in req["system"])

    def test_retries_without_cache_on_unsupported_model(self, provider: BedrockProvider) -> None:
        provider._client.converse.side_effect = [
            Exception("ValidationException: This model does not support the cachePoint block"),
            _ok_response("recovered"),
        ]
        result = provider.invoke_model(
            [{"role": "user", "content": "hi"}],
            system="s",
            tools=TOOLS,
            prompt_caching=True,
        )
        assert result["content"] == "recovered"
        assert provider._client.converse.call_count == 2
        # The retry must not carry cachePoints.
        retry_req = provider._client.converse.call_args_list[1].kwargs
        assert all("cachePoint" not in b for b in retry_req["system"])
        assert all("cachePoint" not in t for t in retry_req["toolConfig"]["tools"])

    def test_cache_usage_metrics_mapped(self, provider: BedrockProvider) -> None:
        provider._client.converse.return_value = _ok_response(
            cacheReadInputTokens=100, cacheWriteInputTokens=40
        )
        result = provider.invoke_model(
            [{"role": "user", "content": "hi"}], system="s", prompt_caching=True
        )
        assert result["usage"]["cache_read_input_tokens"] == 100
        assert result["usage"]["cache_creation_input_tokens"] == 40
