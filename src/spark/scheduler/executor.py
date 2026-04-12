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
        self._mcp_manager: Any | None = None
        self._mcp_loop: Any | None = None
        self._mcp_thread: Any | None = None
        from spark.core.user_guid import get_user_guid

        self._user_guid = get_user_guid(ctx)

    def _init_mcp(self) -> None:
        """Initialise independent MCP connections for this executor."""
        import asyncio
        import threading

        import yaml

        from spark.core.application import _get_config_path

        config_path = _get_config_path()
        if not config_path.exists():
            logger.info("Executor MCP: config file not found at %s", config_path)
            return

        try:
            raw_config = yaml.safe_load(config_path.read_text()) or {}
            mcp_config = raw_config.get("mcp", {})
            servers_list = mcp_config.get("servers", [])
            if not servers_list:
                logger.info("Executor MCP: no MCP servers configured in config.yaml")
                return

            enabled_count = sum(1 for s in servers_list if s.get("enabled", True))
            logger.info(
                "Executor MCP: found %d server(s) in config (%d enabled)",
                len(servers_list),
                enabled_count,
            )
            for srv in servers_list:
                logger.info(
                    "  - %s (transport=%s, enabled=%s, command=%s)",
                    srv.get("name", "?"),
                    srv.get("transport", "?"),
                    srv.get("enabled", True),
                    srv.get("command", srv.get("url", "n/a")),
                )

            from spark.mcp_integration.manager import MCPManager

            self._mcp_manager = MCPManager.from_config(raw_config)
            logger.info(
                "Executor MCP: MCPManager created with %d client(s)",
                len(self._mcp_manager.servers),
            )

            # Create a persistent event loop for MCP connections
            self._mcp_loop = asyncio.new_event_loop()

            def _run_loop() -> None:
                asyncio.set_event_loop(self._mcp_loop)
                self._mcp_loop.run_forever()

            self._mcp_thread = threading.Thread(target=_run_loop, daemon=True)
            self._mcp_thread.start()
            logger.info("Executor MCP: event loop thread started")

            # Connect all servers
            logger.info("Executor MCP: connecting to servers...")
            future = asyncio.run_coroutine_threadsafe(
                self._mcp_manager.connect_all(), self._mcp_loop
            )
            results = future.result(timeout=60)
            for name, success in results.items():
                logger.info("  - %s: %s", name, "connected" if success else "FAILED")
            connected = sum(1 for v in results.values() if v)
            failed = sum(1 for v in results.values() if not v)
            logger.info("Executor MCP: %d connected, %d failed", connected, failed)

            # Populate tool cache
            tools_future = asyncio.run_coroutine_threadsafe(
                self._mcp_manager.list_all_tools(), self._mcp_loop
            )
            tools = tools_future.result(timeout=10)
            tool_names = [t.get("name", "?") for t in tools]
            logger.info("Executor MCP: %d tools available: %s", len(tools), ", ".join(tool_names))

        except Exception as e:
            logger.error("Executor MCP init failed: %s", e, exc_info=True)
            self._mcp_manager = None

    def _cleanup_mcp(self) -> None:
        """Disconnect MCP servers and stop the event loop."""
        if self._mcp_manager and self._mcp_loop:
            import asyncio

            logger.info("Executor MCP: disconnecting servers...")
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._mcp_manager.disconnect_all(), self._mcp_loop
                )
                future.result(timeout=10)
                logger.info("Executor MCP: all servers disconnected")
            except Exception as e:
                logger.warning("Executor MCP cleanup error: %s", e)

        if self._mcp_loop:
            self._mcp_loop.call_soon_threadsafe(self._mcp_loop.stop)
            if self._mcp_thread:
                self._mcp_thread.join(timeout=5)
            self._mcp_loop = None

        self._mcp_manager = None

    def execute(self, action_id: int) -> None:
        """Execute an action by ID."""
        from spark.database import autonomous_actions
        from spark.database.backends import create_backend
        from spark.database.connection import DatabaseConnection
        from spark.database.schema import initialise_schema

        backend = create_backend(self._ctx.settings)
        db = DatabaseConnection(backend)
        initialise_schema(db)

        # Initialise MCP connections for this execution
        logger.info("Executor: initialising MCP connections for action %d", action_id)
        self._init_mcp()
        logger.info(
            "Executor: MCP init complete (manager=%s, loop=%s)",
            "ready" if self._mcp_manager else "none",
            "running" if self._mcp_loop and self._mcp_loop.is_running() else "none",
        )

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
            self._cleanup_mcp()
            db.close()

    def _run_action(self, db: Any, action: dict) -> dict[str, Any]:
        """Run the action's prompt through the LLM with tools."""
        model_id = action["model_id"]
        prompt = action["action_prompt"]
        max_tokens = action.get("max_tokens", 8192)
        context_mode = action.get("context_mode", "fresh")

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

        # Build context from previous runs if cumulative mode
        if context_mode == "cumulative":
            previous_context = self._build_cumulative_context(db, action)
            if previous_context:
                system += f"\n\n## Previous Run Results\n\n{previous_context}"

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
                    logger.info(
                        "Action tool call: %s (params=%s)",
                        tool_name,
                        json.dumps(tool_input)[:200],
                    )

                    result_text = self._execute_tool(tool_name, tool_input)
                    logger.info(
                        "Action tool %s result (%d chars): %s",
                        tool_name,
                        len(result_text),
                        result_text[:200],
                    )
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

    def _build_cumulative_context(self, db: Any, action: dict) -> str:
        """Build context from previous completed runs for cumulative mode.

        Returns a formatted string summarising recent run results,
        newest first, capped at 5 runs to avoid context overflow.
        """
        try:
            from spark.database import autonomous_actions

            runs = autonomous_actions.get_action_runs(db, action["id"], limit=5)
            completed = [r for r in runs if r.get("status") == "completed" and r.get("result_text")]

            if not completed:
                return ""

            parts = []
            for i, run in enumerate(completed):
                ts = run.get("completed_at", run.get("started_at", "unknown"))
                result = run.get("result_text", "")
                # Truncate very long results to avoid blowing context
                if len(result) > 2000:
                    result = result[:2000] + "\n... (truncated)"
                parts.append(f"### Run {len(completed) - i} ({ts})\n\n{result}")

            # Reverse so oldest is first (chronological order)
            parts.reverse()
            return "\n\n---\n\n".join(parts)

        except Exception as e:
            logger.warning("Failed to load cumulative context: %s", e)
            return ""

    def _get_tools(self) -> list[dict]:
        """Get available tools for the action (builtin + MCP)."""
        logger.info(
            "Executor _get_tools: mcp_manager=%s, mcp_loop=%s",
            "present" if self._mcp_manager else "none",
            "running" if self._mcp_loop and self._mcp_loop.is_running() else "none",
        )
        try:
            from spark.tools.registry import get_builtin_tools

            config = {"embedded_tools": self._ctx.settings.get("embedded_tools") or {}}
            # Resolve secret:// URIs in embedded tools config
            self._resolve_embedded_secrets(config.get("embedded_tools", {}))
            tools = get_builtin_tools(config)

            # Add MCP server tools if configured
            if self._mcp_manager:
                try:
                    import asyncio

                    mcp_tools = asyncio.run_coroutine_threadsafe(
                        self._mcp_manager.list_all_tools(), self._mcp_loop
                    ).result(timeout=10)
                    for t in mcp_tools:
                        tools.append(
                            {
                                "name": t.get("name", ""),
                                "description": t.get("description", ""),
                                "inputSchema": t.get("inputSchema", {}),
                            }
                        )
                    if mcp_tools:
                        logger.info("Daemon loaded %d MCP tools", len(mcp_tools))
                except Exception as e:
                    logger.warning("Failed to load MCP tools in daemon: %s", e)

            return tools
        except Exception as e:
            logger.warning("Failed to load tools: %s", e)
            return []

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool and return the result text."""
        try:
            from spark.tools.registry import execute_builtin_tool

            config = {"embedded_tools": self._ctx.settings.get("embedded_tools") or {}}
            # Resolve secret:// URIs in embedded tools config
            self._resolve_embedded_secrets(config.get("embedded_tools", {}))

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

            # Try builtin tools first
            from spark.tools.registry import get_builtin_tools

            builtin_names = {t["name"] for t in get_builtin_tools(config)}
            if tool_name in builtin_names:
                result, is_error = execute_builtin_tool(tool_name, tool_input, config)
                if is_error:
                    logger.warning("Action tool %s error: %s", tool_name, result[:200])
                return result

            # Try MCP tools
            if self._mcp_manager:
                try:
                    import asyncio

                    future = asyncio.run_coroutine_threadsafe(
                        self._mcp_manager.call_tool(tool_name, tool_input),
                        self._mcp_loop,
                    )
                    mcp_result = future.result(timeout=60)
                    content = mcp_result.get("content", [])
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                        elif isinstance(item, str):
                            text_parts.append(item)
                    return "\n".join(text_parts) if text_parts else str(mcp_result)
                except Exception as e:
                    logger.warning("MCP tool %s failed in daemon: %s", tool_name, e)
                    return f"MCP tool error: {e}"

            return f"Tool '{tool_name}' is not available."
        except Exception as e:
            return f"Tool error: {e}"

    def _resolve_embedded_secrets(self, embedded: dict) -> None:
        """Resolve secret:// URIs in embedded tools config subtrees in-place."""
        for _category, cat_config in embedded.items():
            if isinstance(cat_config, dict):
                for key, val in cat_config.items():
                    if isinstance(val, str) and val.startswith("secret://"):
                        cat_config[key] = self._resolve_secret(val)

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
