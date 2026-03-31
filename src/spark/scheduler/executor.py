"""Action executor — runs a single autonomous action with LLM and tools."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from konfig import AppContext

logger = logging.getLogger(__name__)


class ActionExecutor:
    """Executes a single autonomous action."""

    def __init__(self, ctx: Any, daemon_id: str) -> None:
        self._ctx = ctx
        self._daemon_id = daemon_id
        from spark.core.user_guid import get_user_guid

        self._user_guid = get_user_guid(ctx)

    def execute(self, action_id: int) -> None:
        """Execute an action by ID."""
        from spark.database import autonomous_actions
        from spark.database.backends import create_backend
        from spark.database.connection import DatabaseConnection
        from spark.database.schema import initialise_schema

        backend = create_backend(self._ctx.settings)
        db = DatabaseConnection(backend)
        initialise_schema(db)

        try:
            action = autonomous_actions.get_action(db, action_id, self._user_guid)
            if not action:
                logger.error("Action %d not found", action_id)
                return

            if not action.get("is_enabled"):
                logger.info("Action '%s' is disabled, skipping", action["name"])
                return

            # Try to acquire lock
            if not autonomous_actions.try_lock_action(db, action_id, self._daemon_id):
                logger.info("Action '%s' is locked by another process", action["name"])
                return

            logger.info(
                "Executing action: '%s' (model: %s)", action["name"], action.get("model_id")
            )
            run_id = autonomous_actions.start_run(db, action_id, self._user_guid)

            try:
                result = self._run_action(db, action)

                autonomous_actions.complete_run(
                    db,
                    run_id,
                    status="completed",
                    result_text=result.get("content", ""),
                    input_tokens=result.get("input_tokens", 0),
                    output_tokens=result.get("output_tokens", 0),
                )
                logger.info(
                    "Action '%s' completed (%d in / %d out tokens)",
                    action["name"],
                    result.get("input_tokens", 0),
                    result.get("output_tokens", 0),
                )

                # Reset failure count on success
                autonomous_actions.update_action(db, action_id, self._user_guid, failure_count=0)

            except Exception as e:
                logger.error("Action '%s' failed: %s", action["name"], e, exc_info=True)
                autonomous_actions.complete_run(
                    db,
                    run_id,
                    status="failed",
                    error_message=str(e),
                )

                # Increment failure count
                new_count = action.get("failure_count", 0) + 1
                max_failures = action.get("max_failures", 3)
                updates: dict[str, Any] = {"failure_count": new_count}
                if new_count >= max_failures:
                    updates["is_enabled"] = 0
                    logger.warning(
                        "Action '%s' disabled after %d consecutive failures",
                        action["name"],
                        new_count,
                    )
                autonomous_actions.update_action(db, action_id, self._user_guid, **updates)

            finally:
                autonomous_actions.unlock_action(db, action_id)

        finally:
            db.close()

    def _run_action(self, db: Any, action: dict) -> dict[str, Any]:
        """Run the action's prompt through the LLM with tools."""
        model_id = action["model_id"]
        prompt = action["action_prompt"]
        max_tokens = action.get("max_tokens", 8192)

        # Initialise LLM
        llm = self._init_llm(model_id)
        if not llm:
            raise RuntimeError(
                f"Could not initialise LLM for model '{model_id}'. Check provider is enabled and API key is configured."
            )

        # Get available tools
        tools = self._get_tools()

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        system = (
            f"You are Spark executing an autonomous action.\n"
            f"Action: {action['name']}\n"
            f"Description: {action.get('description', '')}\n"
            f"Current time: {now}\n\n"
            f"You have access to tools. Use them as needed to complete the action."
        )

        messages: list[dict] = [{"role": "user", "content": prompt}]

        # Tool use loop (max 10 iterations)
        total_input = 0
        total_output = 0
        for iteration in range(10):
            response = llm.invoke_model(
                messages,
                max_tokens=max_tokens,
                temperature=0.7,
                tools=tools if tools else None,
                system=system,
            )

            usage = response.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)

            stop_reason = response.get("stop_reason", "end_turn")

            if stop_reason == "tool_use" and response.get("tool_use"):
                # Execute tools
                tool_results = []
                for tc in response["tool_use"]:
                    tool_name = tc.get("name", "")
                    tool_input = tc.get("input", {})
                    logger.info("Action tool call: %s", tool_name)

                    result_text = self._execute_tool(tool_name, tool_input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.get("id", ""),
                            "content": result_text,
                        }
                    )

                # Add to conversation
                messages.append(
                    {"role": "assistant", "content": response.get("content_blocks", [])}
                )
                messages.append({"role": "user", "content": tool_results})
                continue

            # Final response
            return {
                "content": response.get("content", ""),
                "input_tokens": total_input,
                "output_tokens": total_output,
            }

        return {
            "content": "Max tool iterations reached.",
            "input_tokens": total_input,
            "output_tokens": total_output,
        }

    def _get_tools(self) -> list[dict]:
        """Get available tools for the action."""
        try:
            from spark.tools.registry import get_builtin_tools

            config = {"embedded_tools": self._ctx.settings.get("embedded_tools") or {}}
            return get_builtin_tools(config)
        except Exception as e:
            logger.warning("Failed to load tools: %s", e)
            return []

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool and return the result text."""
        try:
            from spark.tools.registry import execute_builtin_tool

            config = {"embedded_tools": self._ctx.settings.get("embedded_tools") or {}}

            # Inject memory index for memory tools
            try:
                from spark.database.backends import create_backend
                from spark.database.connection import DatabaseConnection
                from spark.database.schema import initialise_schema
                from spark.index.memory_index import MemoryIndex

                backend = create_backend(self._ctx.settings)
                db = DatabaseConnection(backend)
                initialise_schema(db)
                config["_memory_index"] = MemoryIndex(db, self._user_guid)
            except Exception:
                pass

            result, is_error = execute_builtin_tool(tool_name, tool_input, config)
            if is_error:
                logger.warning("Action tool %s error: %s", tool_name, result[:200])
            return result
        except Exception as e:
            return f"Tool error: {e}"

    def _resolve_secret(self, value: str | None) -> str:
        """Resolve a secret:// URI."""
        if not value:
            return ""
        if hasattr(self._ctx, "secrets") and self._ctx.secrets:
            resolved = self._ctx.secrets.resolve_uri(value)
            return str(resolved) if resolved else ""
        return str(value)

    def _init_llm(self, model_id: str) -> Any:
        """Initialise an LLM service for the given model."""
        settings = self._ctx.settings

        providers = [
            ("anthropic", self._init_anthropic),
            ("aws_bedrock", self._init_bedrock),
            ("ollama", self._init_ollama),
            ("google_gemini", self._init_gemini),
            ("xai", self._init_xai),
        ]

        for key, init_fn in providers:
            if settings.get(f"providers.{key}.enabled"):
                try:
                    service = init_fn(settings)
                    service.set_model(model_id)
                    logger.info("Initialised %s provider for model %s", key, model_id)
                    return service
                except Exception as e:
                    logger.debug("Provider %s failed for model %s: %s", key, model_id, e)
                    continue

        logger.error(
            "No provider could initialise model '%s'. Enabled providers: %s",
            model_id,
            [k for k, _ in providers if settings.get(f"providers.{k}.enabled")],
        )
        return None

    def _init_anthropic(self, settings: Any) -> Any:
        from spark.llm.anthropic_direct import AnthropicDirectProvider

        api_key = self._resolve_secret(settings.get("providers.anthropic.api_key", ""))
        if not api_key:
            raise ValueError("Anthropic API key not configured")
        return AnthropicDirectProvider(api_key=api_key)

    def _init_bedrock(self, settings: Any) -> Any:
        from spark.llm.bedrock import BedrockProvider

        return BedrockProvider(
            region=settings.get("providers.aws_bedrock.region", "us-east-1"),
            profile=settings.get("providers.aws_bedrock.profile"),
        )

    def _init_ollama(self, settings: Any) -> Any:
        from spark.llm.ollama import OllamaProvider

        return OllamaProvider(
            base_url=settings.get("providers.ollama.base_url", "http://localhost:11434")
        )

    def _init_gemini(self, settings: Any) -> Any:
        from spark.llm.google_gemini import GoogleGeminiProvider

        api_key = self._resolve_secret(settings.get("providers.google_gemini.api_key", ""))
        if not api_key:
            raise ValueError("Google Gemini API key not configured")
        return GoogleGeminiProvider(api_key=api_key)

    def _init_xai(self, settings: Any) -> Any:
        from spark.llm.xai import XAIProvider

        api_key = self._resolve_secret(settings.get("providers.xai.api_key", ""))
        if not api_key:
            raise ValueError("X.AI API key not configured")
        return XAIProvider(api_key=api_key)
