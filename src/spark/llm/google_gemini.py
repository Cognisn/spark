"""Google Gemini LLM provider."""

from __future__ import annotations

import logging
import time
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)

# Fallback static list used if the API listing fails
_FALLBACK_MODELS = [
    {
        "id": "gemini-2.5-pro-preview-06-05",
        "name": "Gemini 2.5 Pro",
        "context_length": 1_000_000,
        "max_output": 65_536,
    },
    {
        "id": "gemini-2.5-flash-preview-05-20",
        "name": "Gemini 2.5 Flash",
        "context_length": 1_000_000,
        "max_output": 65_536,
    },
    {
        "id": "gemini-2.0-flash",
        "name": "Gemini 2.0 Flash",
        "context_length": 1_000_000,
        "max_output": 8_192,
    },
    {
        "id": "gemini-1.5-pro",
        "name": "Gemini 1.5 Pro",
        "context_length": 2_000_000,
        "max_output": 8_192,
    },
    {
        "id": "gemini-1.5-flash",
        "name": "Gemini 1.5 Flash",
        "context_length": 1_000_000,
        "max_output": 8_192,
    },
]


class GoogleGeminiProvider(LLMService):
    """Google Gemini API provider."""

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        rate_limit_base_delay: float = 2.0,
    ) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model_id: str | None = None
        self._max_retries = max_retries
        self._base_delay = rate_limit_base_delay
        self._cached_models: list[dict[str, Any]] | None = None

    def get_provider_name(self) -> str:
        return "Google Gemini"

    def get_access_info(self) -> str:
        return "Google Gemini API"

    def list_available_models(self) -> list[dict[str, Any]]:
        """List models from the Gemini API, with static fallback."""
        if self._cached_models is not None:
            return self._cached_models

        try:
            models: list[dict[str, Any]] = []
            for m in self._client.models.list():
                model_id = m.name
                # Strip "models/" prefix if present
                if model_id.startswith("models/"):
                    model_id = model_id[7:]

                display_name = getattr(m, "display_name", model_id) or model_id
                input_limit = getattr(m, "input_token_limit", None) or 0
                output_limit = getattr(m, "output_token_limit", None) or 0

                # Only include generateContent-capable models
                methods = getattr(m, "supported_generation_methods", []) or []
                if methods and "generateContent" not in methods:
                    continue

                models.append(
                    {
                        "id": model_id,
                        "name": display_name,
                        "provider": "Google Gemini",
                        "supports_tools": True,
                        "context_length": input_limit,
                        "max_output": output_limit,
                    }
                )

            if models:
                self._cached_models = models
                logger.info("Discovered %d Gemini models from API", len(models))
                return models

        except Exception as e:
            logger.warning("Failed to list Gemini models from API, using fallback: %s", e)

        # Fallback to static list
        self._cached_models = [
            {**m, "provider": "Google Gemini", "supports_tools": True} for m in _FALLBACK_MODELS
        ]
        return self._cached_models

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id

    def supports_streaming(self) -> bool:
        return False

    def count_tokens(self, text: str) -> int:
        try:
            from google import genai

            resp = self._client.models.count_tokens(
                model=self._model_id or "gemini-1.5-flash",
                contents=text,
            )
            return resp.total_tokens
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

        from google.genai import types

        gemini_contents = _convert_messages(messages)
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            config["system_instruction"] = system

        req_kwargs: dict[str, Any] = {
            "model": self._model_id,
            "contents": gemini_contents,
            "config": types.GenerateContentConfig(**config),
        }

        if tools:
            req_kwargs["config"] = types.GenerateContentConfig(
                **config,
                tools=_convert_tools(tools),
            )

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.models.generate_content(**req_kwargs)
                return _normalise_response(response)
            except Exception as e:
                err = str(e).lower()
                retryable = (
                    "rate" in err
                    or "429" in err
                    or "quota" in err
                    or "503" in err
                    or "unavailable" in err
                    or "500" in err
                    or "overloaded" in err
                )
                if retryable and attempt < self._max_retries:
                    delay = self._base_delay ** (attempt + 1)
                    logger.warning("Transient error, retrying in %.1fs: %s", delay, e)
                    time.sleep(delay)
                    continue
                logger.error("Gemini invocation failed: %s", e)
                return {
                    "content": "",
                    "stop_reason": "error",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "tool_use": None,
                    "content_blocks": [],
                    "error": True,
                    "error_message": str(e),
                }

        return {
            "content": "",
            "stop_reason": "error",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "tool_use": None,
            "content_blocks": [],
        }


def _normalise_response(response: Any) -> dict[str, Any]:
    """Convert Gemini response to standard format."""
    text_parts: list[str] = []
    tool_blocks: list[dict] = []
    content_blocks: list[dict] = []

    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            if hasattr(part, "text") and part.text:
                text_parts.append(part.text)
                content_blocks.append({"type": "text", "text": part.text})
            elif hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                tool = {
                    "type": "tool_use",
                    "id": f"gemini_{fc.name}",
                    "name": fc.name,
                    "input": dict(fc.args) if fc.args else {},
                }
                tool_blocks.append(tool)
                content_blocks.append(tool)

    usage = {"input_tokens": 0, "output_tokens": 0}
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        um = response.usage_metadata
        usage["input_tokens"] = getattr(um, "prompt_token_count", 0) or 0
        usage["output_tokens"] = getattr(um, "candidates_token_count", 0) or 0

    stop_reason = "tool_use" if tool_blocks else "end_turn"

    return {
        "content": "".join(text_parts),
        "stop_reason": stop_reason,
        "usage": usage,
        "tool_use": tool_blocks if tool_blocks else None,
        "content_blocks": content_blocks,
    }


def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard messages to Gemini format."""
    from google.genai import types

    converted = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else msg["role"]
        content = msg["content"]
        parts = []

        if isinstance(content, str):
            parts.append(types.Part(text=content))
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(types.Part(text=block))
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(types.Part(text=block["text"]))
                    elif block.get("type") == "tool_use":
                        parts.append(
                            types.Part(
                                function_call=types.FunctionCall(
                                    name=block["name"],
                                    args=block.get("input", {}),
                                )
                            )
                        )
                    elif block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, list):
                            result = " ".join(
                                b.get("text", "") for b in result if isinstance(b, dict)
                            )
                        parts.append(
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=block.get("name", "unknown"),
                                    response={"result": str(result)},
                                )
                            )
                        )

        if parts:
            converted.append(types.Content(role=role, parts=parts))

    return converted


def _convert_tools(tools: list[dict[str, Any]]) -> list[Any]:
    """Convert standard tool format to Gemini format."""
    from google.genai import types

    declarations = []
    for t in tools:
        schema = t.get("inputSchema") or t.get("input_schema", {})
        schema = _clean_schema(schema)
        declarations.append(
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=schema,
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _clean_schema(schema: dict) -> dict:
    """Remove fields unsupported by Gemini's API."""
    unsupported = {
        "additionalProperties",
        "$ref",
        "$defs",
        "default",
        "examples",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
    }
    cleaned = {k: v for k, v in schema.items() if k not in unsupported}
    if "properties" in cleaned:
        cleaned["properties"] = {k: _clean_schema(v) for k, v in cleaned["properties"].items()}
    if "items" in cleaned and isinstance(cleaned["items"], dict):
        cleaned["items"] = _clean_schema(cleaned["items"])
    return cleaned
