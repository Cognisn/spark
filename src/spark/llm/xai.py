"""X.AI (Grok) LLM provider via OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)

_MODELS = [
    {"id": "grok-4.1-fast", "name": "Grok 4.1 Fast", "context_length": 2_000_000, "max_output": 131_072},
    {"id": "grok-4-0709", "name": "Grok 4", "context_length": 256_000, "max_output": 16_384},
    {"id": "grok-3", "name": "Grok 3", "context_length": 131_072, "max_output": 8_192},
    {"id": "grok-3-mini", "name": "Grok 3 Mini", "context_length": 131_072, "max_output": 8_192},
]


class XAIProvider(LLMService):
    """X.AI / Grok provider using OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.x.ai/v1",
        max_retries: int = 3,
        rate_limit_base_delay: float = 2.0,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model_id: str | None = None
        self._max_retries = max_retries
        self._base_delay = rate_limit_base_delay

    def get_provider_name(self) -> str:
        return "X.AI"

    def get_access_info(self) -> str:
        return "X.AI API (api.x.ai)"

    def list_available_models(self) -> list[dict[str, Any]]:
        return [
            {**m, "provider": "X.AI", "supports_tools": True}
            for m in _MODELS
        ]

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id

    def supports_streaming(self) -> bool:
        return False

    def count_tokens(self, text: str) -> int:
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
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

        openai_messages = _convert_messages(messages, system=system)
        req: dict[str, Any] = {
            "model": self._model_id,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            req["tools"] = _convert_tools(tools)

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.chat.completions.create(**req)
                return _normalise_response(response)
            except Exception as e:
                err = str(e).lower()
                if ("rate" in err or "429" in err) and attempt < self._max_retries:
                    delay = self._base_delay ** (attempt + 1)
                    logger.warning("Rate limited, retrying in %.1fs", delay)
                    time.sleep(delay)
                    continue
                logger.error("X.AI invocation failed: %s", e)
                return {
                    "content": "",
                    "stop_reason": "error",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "tool_use": None,
                    "content_blocks": [],
                    "error": True,
                    "error_message": str(e),
                }

        return {"content": "", "stop_reason": "error", "usage": {"input_tokens": 0, "output_tokens": 0},
                "tool_use": None, "content_blocks": []}


def _normalise_response(response: Any) -> dict[str, Any]:
    """Convert OpenAI-format response to standard format."""
    choice = response.choices[0] if response.choices else None
    if not choice:
        return {
            "content": "",
            "stop_reason": "error",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "tool_use": None,
            "content_blocks": [],
        }

    text = choice.message.content or ""
    tool_blocks: list[dict] = []
    content_blocks: list[dict] = [{"type": "text", "text": text}]

    for tc in choice.message.tool_calls or []:
        args = tc.function.arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}

        tool = {
            "type": "tool_use",
            "id": tc.id,
            "name": tc.function.name,
            "input": args,
        }
        tool_blocks.append(tool)
        content_blocks.append(tool)

    # Map stop reasons
    finish = choice.finish_reason or "stop"
    stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
    stop_reason = stop_map.get(finish, finish)

    usage = {"input_tokens": 0, "output_tokens": 0}
    if response.usage:
        usage["input_tokens"] = response.usage.prompt_tokens or 0
        usage["output_tokens"] = response.usage.completion_tokens or 0

    return {
        "content": text,
        "stop_reason": stop_reason,
        "usage": usage,
        "tool_use": tool_blocks if tool_blocks else None,
        "content_blocks": content_blocks,
    }


def _convert_messages(
    messages: list[dict[str, Any]], *, system: str | None = None
) -> list[dict[str, Any]]:
    """Convert standard messages to OpenAI format."""
    converted = []
    if system:
        converted.append({"role": "system", "content": system})

    # Build tool_id→name map
    tool_map: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_map[block["id"]] = block["name"]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            converted.append({"role": role, "content": content})
        elif isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict] = []

            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
                    elif block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, list):
                            result = " ".join(
                                b.get("text", "") for b in result if isinstance(b, dict)
                            )
                        converted.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": str(result),
                        })
                        continue

            if text_parts or tool_calls:
                m: dict[str, Any] = {"role": role, "content": "\n".join(text_parts) if text_parts else ""}
                if tool_calls:
                    m["tool_calls"] = tool_calls
                converted.append(m)

    return converted


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard tool format to OpenAI format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema") or t.get("input_schema", {}),
            },
        }
        for t in tools
    ]
