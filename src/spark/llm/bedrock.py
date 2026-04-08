"""AWS Bedrock LLM provider."""

from __future__ import annotations

import json
import logging
from typing import Any

from spark.llm.base import LLMService

logger = logging.getLogger(__name__)


class BedrockProvider(LLMService):
    """AWS Bedrock provider using boto3."""

    def __init__(
        self,
        region: str = "us-east-1",
        *,
        profile: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        session_token: str | None = None,
    ) -> None:
        import boto3

        session_kwargs: dict[str, Any] = {"region_name": region}
        if access_key and secret_key:
            # Explicit IAM / session credentials supplied via settings.
            session_kwargs["aws_access_key_id"] = access_key
            session_kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                session_kwargs["aws_session_token"] = session_token
        elif profile:
            session_kwargs["profile_name"] = profile
        session = boto3.Session(**session_kwargs)
        self._client = session.client("bedrock-runtime")
        self._bedrock = session.client("bedrock")
        self._region = region
        self._model_id: str | None = None
        self._cached_models: list[dict[str, Any]] | None = None

    def get_provider_name(self) -> str:
        return "AWS Bedrock"

    def get_access_info(self) -> str:
        return f"AWS Bedrock ({self._region})"

    def list_available_models(self) -> list[dict[str, Any]]:
        """List models available in Bedrock (cached after first call)."""
        if self._cached_models is not None:
            return self._cached_models
        try:
            resp = self._bedrock.list_foundation_models()
            models = []
            for m in resp.get("modelSummaries", []):
                model_id = m.get("modelId", "")
                models.append(
                    {
                        "id": model_id,
                        "name": m.get("modelName", model_id),
                        "provider": "AWS Bedrock",
                        "supports_tools": "anthropic" in model_id.lower(),
                        "context_length": 200_000 if "claude" in model_id.lower() else 8_192,
                    }
                )
            self._cached_models = models
            return models
        except Exception as e:
            logger.error("Failed to list Bedrock models: %s", e)
            return []

    def set_model(self, model_id: str) -> None:
        self._model_id = model_id

    def supports_streaming(self) -> bool:
        return True

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

        # Build Converse API request
        converse_messages = _convert_messages(messages)
        req: dict[str, Any] = {
            "modelId": self._model_id,
            "messages": converse_messages,
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            req["system"] = [{"text": system}]
        if tools:
            req["toolConfig"] = {"tools": _convert_tools(tools)}

        try:
            if stream_callback:
                response = self._client.converse_stream(**req)
                return self._process_stream(response, stream_callback)
            else:
                response = self._client.converse(**req)
                return _normalise_response(response)
        except Exception as e:
            logger.error("Bedrock invocation failed: %s", e)
            return {
                "content": "",
                "stop_reason": "error",
                "usage": {"input_tokens": 0, "output_tokens": 0},
                "tool_use": None,
                "content_blocks": [],
                "error": True,
                "error_message": str(e),
            }

    def _process_stream(self, response: Any, callback: Any) -> dict[str, Any]:
        """Process a Bedrock Converse streaming response."""
        text_parts: list[str] = []
        tool_blocks: list[dict] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        stop_reason = "end_turn"
        current_tool: dict | None = None
        tool_input_json = ""

        for event in response.get("stream", []):
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    current_tool = {
                        "type": "tool_use",
                        "id": start["toolUse"]["toolUseId"],
                        "name": start["toolUse"]["name"],
                        "input": {},
                    }
                    tool_input_json = ""

            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    text_parts.append(delta["text"])
                    callback(delta["text"])
                elif "toolUse" in delta and current_tool:
                    tool_input_json += delta["toolUse"].get("input", "")

            elif "contentBlockStop" in event:
                if current_tool:
                    try:
                        current_tool["input"] = (
                            json.loads(tool_input_json) if tool_input_json else {}
                        )
                    except json.JSONDecodeError:
                        current_tool["input"] = {}
                    tool_blocks.append(current_tool)
                    current_tool = None

            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", "end_turn")

            elif "metadata" in event:
                u = event["metadata"].get("usage", {})
                usage["input_tokens"] = u.get("inputTokens", 0)
                usage["output_tokens"] = u.get("outputTokens", 0)

        text = "".join(text_parts)
        content_blocks: list[dict] = [{"type": "text", "text": text}] + tool_blocks

        return {
            "content": text,
            "stop_reason": _map_stop_reason(stop_reason),
            "usage": usage,
            "tool_use": tool_blocks if tool_blocks else None,
            "content_blocks": content_blocks,
        }


def _normalise_response(response: dict[str, Any]) -> dict[str, Any]:
    """Convert Bedrock Converse response to standard format."""
    output = response.get("output", {})
    message = output.get("message", {})
    content = message.get("content", [])

    text_parts: list[str] = []
    tool_blocks: list[dict] = []
    content_blocks: list[dict] = []

    for block in content:
        if "text" in block:
            text_parts.append(block["text"])
            content_blocks.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tool = {
                "type": "tool_use",
                "id": block["toolUse"]["toolUseId"],
                "name": block["toolUse"]["name"],
                "input": block["toolUse"].get("input", {}),
            }
            tool_blocks.append(tool)
            content_blocks.append(tool)

    usage_data = response.get("usage", {})
    stop = response.get("stopReason", "end_turn")

    return {
        "content": "".join(text_parts),
        "stop_reason": _map_stop_reason(stop),
        "usage": {
            "input_tokens": usage_data.get("inputTokens", 0),
            "output_tokens": usage_data.get("outputTokens", 0),
        },
        "tool_use": tool_blocks if tool_blocks else None,
        "content_blocks": content_blocks,
    }


def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard messages to Bedrock Converse format."""
    converted = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            converted.append({"role": role, "content": [{"text": content}]})
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, str):
                    blocks.append({"text": block})
                elif block.get("type") == "text":
                    blocks.append({"text": block["text"]})
                elif block.get("type") == "tool_use":
                    blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": block["id"],
                                "name": block["name"],
                                "input": block.get("input", {}),
                            }
                        }
                    )
                elif block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_text = " ".join(
                            b.get("text", "") for b in result_content if isinstance(b, dict)
                        )
                    else:
                        result_text = str(result_content)
                    blocks.append(
                        {
                            "toolResult": {
                                "toolUseId": block["tool_use_id"],
                                "content": [{"text": result_text}],
                            }
                        }
                    )
                else:
                    blocks.append({"text": str(block)})
            if blocks:
                converted.append({"role": role, "content": blocks})
        else:
            converted.append({"role": role, "content": [{"text": str(content)}]})

    return converted


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert standard tool format to Bedrock Converse format."""
    converted = []
    for tool in tools:
        schema = tool.get("inputSchema") or tool.get("input_schema", {})
        if "type" not in schema:
            schema["type"] = "object"
        converted.append(
            {
                "toolSpec": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": {"json": schema},
                }
            }
        )
    return converted


def _map_stop_reason(reason: str) -> str:
    """Map Bedrock stop reasons to standard format."""
    mapping = {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
        "stop_sequence": "end_turn",
    }
    return mapping.get(reason, reason)
