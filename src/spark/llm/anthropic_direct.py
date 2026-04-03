"""Anthropic Direct API provider."""

from __future__ import annotations

import logging
import time
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)

_MODELS = [
    {
        "id": "claude-opus-4-6-20250605",
        "name": "Claude Opus 4.6",
        "context_length": 200_000,
        "max_output": 32_000,
    },
    {
        "id": "claude-sonnet-4-6-20250605",
        "name": "Claude Sonnet 4.6",
        "context_length": 200_000,
        "max_output": 32_000,
    },
    {
        "id": "claude-opus-4-20250514",
        "name": "Claude Opus 4",
        "context_length": 200_000,
        "max_output": 32_000,
    },
    {
        "id": "claude-sonnet-4-20250514",
        "name": "Claude Sonnet 4",
        "context_length": 200_000,
        "max_output": 32_000,
    },
    {
        "id": "claude-3-7-sonnet-20250219",
        "name": "Claude 3.7 Sonnet",
        "context_length": 200_000,
        "max_output": 32_000,
    },
    {
        "id": "claude-3-5-sonnet-20241022",
        "name": "Claude 3.5 Sonnet",
        "context_length": 200_000,
        "max_output": 8_192,
    },
    {
        "id": "claude-3-5-haiku-20241022",
        "name": "Claude 3.5 Haiku",
        "context_length": 200_000,
        "max_output": 8_192,
    },
    {
        "id": "claude-3-opus-20240229",
        "name": "Claude 3 Opus",
        "context_length": 200_000,
        "max_output": 4_096,
    },
    {
        "id": "claude-3-haiku-20240307",
        "name": "Claude 3 Haiku",
        "context_length": 200_000,
        "max_output": 4_096,
    },
]


class AnthropicDirectProvider(LLMService):
    """Anthropic Direct API via the official SDK."""

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 5,
        rate_limit_base_delay: float = 2.0,
    ) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._api_key = api_key
        self._model_id: str | None = None
        self._max_retries = max_retries
        self._base_delay = rate_limit_base_delay
        self._cached_models: list[dict[str, Any]] | None = None

    def get_provider_name(self) -> str:
        return "Anthropic"

    def get_access_info(self) -> str:
        return "Direct API (api.anthropic.com)"

    def list_available_models(self) -> list[dict[str, Any]]:
        """List models from the Anthropic API, with static fallback."""
        if self._cached_models is not None:
            return self._cached_models

        try:
            models = []
            page = self._client.models.list(limit=100)
            for m in page.data:
                model_id = m.id
                display_name = getattr(m, "display_name", model_id)
                models.append(
                    {
                        "id": model_id,
                        "name": display_name,
                        "provider": "Anthropic",
                        "supports_tools": True,
                        "context_length": 200_000,
                    }
                )
            if models:
                self._cached_models = models
                logger.info("Discovered %d Anthropic models from API", len(models))
                return models
        except Exception as e:
            logger.debug("Anthropic model list API failed, using static fallback: %s", e)

        # Fallback to static list
        self._cached_models = [
            {**m, "provider": "Anthropic", "supports_tools": True} for m in _MODELS
        ]
        return self._cached_models

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id

    def supports_streaming(self) -> bool:
        return True

    def count_tokens(self, text: str) -> int:
        try:
            resp = self._client.messages.count_tokens(
                model=self._model_id or "claude-3-5-sonnet-20241022",
                messages=[{"role": "user", "content": text}],
            )
            return resp.input_tokens
        except Exception:
            return len(text) // 4

    def invoke_model(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        stream_callback: Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._model_id:
            raise RuntimeError("No model selected — call set_model() first")

        # Cap max_tokens to model limit
        model_max = _get_model_max_output(self._model_id)
        max_tokens = min(max_tokens, model_max)

        # Build request kwargs
        prompt_caching = kwargs.get("prompt_caching", False)

        req: dict[str, Any] = {
            "model": self._model_id,
            "messages": _clean_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            if prompt_caching:
                # Use Anthropic's prompt caching: wrap system in a block with cache_control
                req["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                req["system"] = system
        if tools:
            converted = _convert_tools(tools)
            if prompt_caching and converted:
                # Cache the tool definitions too (they rarely change within a conversation)
                converted[-1]["cache_control"] = {"type": "ephemeral"}
            req["tools"] = converted

        # Retry loop with exponential backoff for rate limits
        for attempt in range(self._max_retries + 1):
            try:
                if stream_callback:
                    return self._invoke_streaming(req, stream_callback)
                else:
                    return self._invoke_sync(req)
            except Exception as e:
                err = str(e).lower()
                retryable = (
                    "rate" in err
                    or "429" in err
                    or "503" in err
                    or "overloaded" in err
                    or "unavailable" in err
                    or "500" in err
                    or "internal" in err
                )
                if retryable and attempt < self._max_retries:
                    delay = self._base_delay ** (attempt + 1)
                    logger.warning(
                        "Transient error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        self._max_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue
                raise

        return {
            "content": "",
            "stop_reason": "error",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "tool_use": None,
            "content_blocks": [],
            "error": True,
            "error_message": "Max retries exceeded",
        }

    def _invoke_sync(self, req: dict[str, Any]) -> dict[str, Any]:
        response = self._client.messages.create(**req)
        return _normalise_response(response)

    def _invoke_streaming(self, req: dict[str, Any], callback: Any) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_blocks: list[dict] = []
        content_blocks: list[dict] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        stop_reason = "end_turn"

        with self._client.messages.stream(**req) as stream:
            for event in stream:
                event_type = getattr(event, "type", "")

                if event_type == "content_block_start":
                    block = event.content_block
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_blocks.append(
                            {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": {},
                            }
                        )

                elif event_type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "text"):
                        text_parts.append(delta.text)
                        callback(delta.text)
                    elif hasattr(delta, "partial_json") and tool_blocks:
                        # Accumulate tool input JSON
                        pass

                elif event_type == "message_delta":
                    if hasattr(event, "delta"):
                        sr = getattr(event.delta, "stop_reason", None)
                        if sr:
                            stop_reason = sr
                    if hasattr(event, "usage"):
                        usage["output_tokens"] = getattr(event.usage, "output_tokens", 0)

                elif event_type == "message_start":
                    if hasattr(event, "message") and hasattr(event.message, "usage"):
                        usage["input_tokens"] = event.message.usage.input_tokens

            # Get final message
            final = stream.get_final_message()
            if final:
                return _normalise_response(final)

        text = "".join(text_parts)
        content_blocks = [{"type": "text", "text": text}] + tool_blocks

        return {
            "content": text,
            "stop_reason": stop_reason,
            "usage": usage,
            "tool_use": tool_blocks if tool_blocks else None,
            "content_blocks": content_blocks,
        }


def _normalise_response(response: Any) -> dict[str, Any]:
    """Convert Anthropic SDK response to standard format."""
    text_parts: list[str] = []
    tool_blocks: list[dict] = []
    content_blocks: list[dict] = []

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
            content_blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            tool = {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
            tool_blocks.append(tool)
            content_blocks.append(tool)

    return {
        "content": "".join(text_parts),
        "stop_reason": response.stop_reason or "end_turn",
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
        "tool_use": tool_blocks if tool_blocks else None,
        "content_blocks": content_blocks,
    }


def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean messages for Anthropic API compatibility."""
    cleaned = []
    for msg in messages:
        m = {"role": msg["role"], "content": msg["content"]}
        cleaned.append(m)
    return cleaned


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard tool format to Anthropic format."""
    converted = []
    for tool in tools:
        schema = tool.get("inputSchema") or tool.get("input_schema", {})
        if "type" not in schema:
            schema["type"] = "object"
        converted.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": schema,
            }
        )
    return converted


def _get_model_max_output(model_id: str) -> int:
    """Get max output tokens for a model."""
    for m in _MODELS:
        if m["id"] == model_id or m["id"] in model_id:
            return m.get("max_output", 4096)
    # Pattern matching
    lower = model_id.lower()
    if "opus-4" in lower or "sonnet-4" in lower or "3-7-sonnet" in lower:
        return 32_000
    if "3-5" in lower:
        return 8_192
    return 4_096
