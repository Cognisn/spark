"""Ollama local model provider."""

from __future__ import annotations

import json
import logging
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)


class OllamaProvider(LLMService):
    """Ollama provider for local models."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        timeout: int = 120,
        verify_ssl: bool = True,
    ) -> None:
        import ollama as ollama_sdk

        client_kwargs: dict[str, Any] = {"host": base_url}
        if not verify_ssl or timeout != 120:
            import httpx

            http_client = httpx.Client(
                base_url=base_url,
                timeout=httpx.Timeout(timeout),
                verify=verify_ssl,
            )
            client_kwargs = {"host": base_url}

        self._client = ollama_sdk.Client(**client_kwargs)
        self._model_id: str | None = None
        self._base_url = base_url
        self._cached_models: list[dict[str, Any]] | None = None

    def get_provider_name(self) -> str:
        return "Ollama"

    def get_access_info(self) -> str:
        return f"Ollama ({self._base_url})"

    def list_available_models(self) -> list[dict[str, Any]]:
        if self._cached_models is not None:
            return self._cached_models
        try:
            resp = self._client.list()
            models = []
            # Handle both dict and object response formats
            model_list = []
            if isinstance(resp, dict):
                model_list = resp.get("models", [])
            elif hasattr(resp, "models"):
                model_list = resp.models or []

            for m in model_list:
                # Handle both dict and object model entries
                if isinstance(m, dict):
                    name = m.get("name", "") or m.get("model", "")
                else:
                    name = getattr(m, "name", "") or getattr(m, "model", "")

                if not name:
                    continue

                models.append(
                    {
                        "id": name,
                        "name": name,
                        "provider": "Ollama",
                        "supports_tools": _supports_tools(name),
                        "context_length": _estimate_context(name),
                    }
                )
            self._cached_models = models
            return models
        except Exception as e:
            logger.error("Failed to list Ollama models: %s", e)
            return []

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

        ollama_messages = _convert_messages(messages, system=system)
        req: dict[str, Any] = {
            "model": self._model_id,
            "messages": ollama_messages,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            req["tools"] = _convert_tools(tools)

        try:
            response = self._client.chat(**req)
            return _normalise_response(response)
        except Exception as e:
            logger.error("Ollama invocation failed: %s", e)
            return {
                "content": "",
                "stop_reason": "error",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "tool_use": None,
                "content_blocks": [],
                "error": True,
                "error_message": str(e),
            }


def _normalise_response(response: Any) -> dict[str, Any]:
    """Convert Ollama response to standard format."""
    if isinstance(response, dict):
        msg = response.get("message", {})
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])
    else:
        msg = getattr(response, "message", None)
        content = getattr(msg, "content", "") if msg else ""
        tool_calls = getattr(msg, "tool_calls", []) or []

    text = content or ""
    tool_blocks: list[dict] = []
    content_blocks: list[dict] = [{"type": "text", "text": text}]

    for i, tc in enumerate(tool_calls):
        if isinstance(tc, dict):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
        else:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            args = getattr(fn, "arguments", {}) if fn else {}

        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}

        tool = {
            "type": "tool_use",
            "id": f"ollama_tool_{i}",
            "name": name,
            "input": args,
        }
        tool_blocks.append(tool)
        content_blocks.append(tool)

    # Estimate tokens (Ollama doesn't always provide this)
    input_tokens = 0
    output_tokens = len(text) // 4
    if isinstance(response, dict):
        input_tokens = response.get("prompt_eval_count", 0)
        output_tokens = response.get("eval_count", output_tokens)

    stop_reason = "tool_use" if tool_blocks else "end_turn"

    return {
        "content": text,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "tool_use": tool_blocks if tool_blocks else None,
        "content_blocks": content_blocks,
    }


def _convert_messages(
    messages: list[dict[str, Any]], *, system: str | None = None
) -> list[dict[str, Any]]:
    """Convert standard messages to Ollama format."""
    converted = []
    if system:
        converted.append({"role": "system", "content": system})

    # Build tool_id→name map for tool_result conversion
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
                        tool_calls.append(
                            {
                                "function": {
                                    "name": block["name"],
                                    "arguments": block.get("input", {}),
                                }
                            }
                        )
                    elif block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, list):
                            result = " ".join(
                                b.get("text", "") for b in result if isinstance(b, dict)
                            )
                        tool_name = tool_map.get(block.get("tool_use_id", ""), "unknown")
                        converted.append({"role": "tool", "content": str(result)})
                        continue

            if text_parts or tool_calls:
                m: dict[str, Any] = {"role": role, "content": "\n".join(text_parts)}
                if tool_calls:
                    m["tool_calls"] = tool_calls
                converted.append(m)

    return converted


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard tool format to Ollama format."""
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


def _supports_tools(model_name: str) -> bool:
    """Check if an Ollama model supports function calling."""
    lower = model_name.lower()
    return any(p in lower for p in ["llama3", "mistral", "qwen2", "command-r", "mixtral"])


def _estimate_context(model_name: str) -> int:
    """Estimate context length for an Ollama model."""
    lower = model_name.lower()
    if "llama3" in lower:
        return 128_000
    if "mistral" in lower or "mixtral" in lower:
        return 32_768
    if "qwen2" in lower:
        return 128_000
    if "codellama" in lower:
        return 16_384
    return 8_192
