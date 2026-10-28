"""Agent executor — runs a sub-agent tool-use loop within a conversation."""

from __future__ import annotations

import logging
from typing import Any, Callable

from spark.core.cancellation import CancellationToken

logger = logging.getLogger(__name__)


class AgentExecutor:
    """Executes a sub-agent task with its own message context and tool-use loop."""

    def __init__(
        self,
        llm_service: Any,
        db: Any,
        embedded_tools_config: dict[str, Any],
        *,
        mcp_manager: Any | None = None,
        mcp_loop: Any | None = None,
        user_guid: str = "default",
        tool_permission_callback: Callable | None = None,
        status_callback: Callable | None = None,
    ) -> None:
        self._llm = llm_service
        self._db = db
        self._config = embedded_tools_config
        self._mcp_manager = mcp_manager
        self._mcp_loop = mcp_loop
        self._user_guid = user_guid
        self._tool_permission_callback = tool_permission_callback
        self._status_callback = status_callback

    def execute(
        self,
        agent_id: str,
        agent_name: str,
        task: str,
        model_id: str,
        *,
        mode: str = "orchestrator",
        parent_messages: list[dict] | None = None,
        max_iterations: int = 15,
        max_tokens: int = 8192,
        cancel_token: CancellationToken | None = None,
        system_override: str | None = None,
        exclude_tools: frozenset[str] | set[str] | None = None,
        extra_tools: list[dict] | None = None,
        terminal_tool: str | None = None,
        prompt_caching: bool = True,
    ) -> dict[str, Any]:
        """Run the agent's tool-use loop.

        Returns a dict with: content, input_tokens, output_tokens, tool_calls.
        When terminal_tool is set and the model calls it, the result also
        carries terminal_call = {name, input} and the tool is NOT executed.
        """
        # Set the model for this agent's execution
        self._llm.set_model(model_id)

        # Tools refused at execution time as well as removed from the offer
        self._exclude_tools = frozenset(exclude_tools or ())

        # Build the system prompt for this agent (or use the caller's)
        if system_override is not None:
            system = system_override
        else:
            system = self._build_system(agent_name, task, mode)

        # Build initial messages — chain mode inherits parent context,
        # orchestrator mode starts fresh with just the task.
        if mode == "chain" and parent_messages:
            messages = list(parent_messages)
            messages.append({"role": "user", "content": f"[Agent Task] {task}"})
        else:
            messages = [{"role": "user", "content": task}]

        # Gather available tools (same as parent conversation), then apply
        # the caller's filter and additions.
        tools = self._get_tools()
        if self._exclude_tools:
            tools = [t for t in tools if t.get("name") not in self._exclude_tools]
        if extra_tools:
            tools.extend(extra_tools)

        # Notify that the agent is starting
        if self._status_callback:
            self._status_callback(
                "agent_start",
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "task": task,
                    "model_id": model_id,
                    "mode": mode,
                },
            )

        # Tool-use loop
        total_input = 0
        total_output = 0
        all_tool_calls: list[dict[str, Any]] = []

        def _is_cancelled() -> bool:
            return cancel_token is not None and cancel_token.is_cancelled()

        def _cancelled_result() -> dict[str, Any]:
            if self._status_callback:
                self._status_callback(
                    "agent_complete",
                    {
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "status": "cancelled",
                        "result": "[CANCELLED]",
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                    },
                )
            return {
                "content": "[CANCELLED]",
                "status": "cancelled",
                "input_tokens": total_input,
                "output_tokens": total_output,
                "tool_calls": all_tool_calls,
            }

        # When a terminal tool is required, keep a handle on just that tool so we
        # can force a final submission if the model would otherwise finish without
        # calling it (e.g. a debater that spends its whole budget researching).
        terminal_only_tools = (
            [t for t in tools if t.get("name") == terminal_tool] if terminal_tool else None
        )
        forced_terminal = False

        def _terminal_result(tc: dict[str, Any]) -> dict[str, Any]:
            if self._status_callback:
                self._status_callback(
                    "agent_complete",
                    {
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "status": "completed",
                        "result": "[argument submitted]",
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                    },
                )
            return {
                "content": "",
                "status": "completed",
                "terminal_call": {"name": terminal_tool, "input": tc.get("input", {})},
                "input_tokens": total_input,
                "output_tokens": total_output,
                "tool_calls": all_tool_calls,
            }

        for iteration in range(max_iterations):
            if _is_cancelled():
                return _cancelled_result()
            response = self._llm.invoke_model(
                messages,
                max_tokens=max_tokens,
                temperature=0.7,
                tools=tools if tools else None,
                system=system,
                prompt_caching=prompt_caching,
            )

            usage = response.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)

            stop_reason = response.get("stop_reason", "end_turn")

            if stop_reason == "tool_use" and response.get("tool_use"):
                # A terminal tool ends the turn: capture its input, do not execute it.
                if terminal_tool:
                    for tc in response["tool_use"]:
                        if tc.get("name") == terminal_tool:
                            return _terminal_result(tc)

                tool_results = []
                for tc in response["tool_use"]:
                    tool_name = tc.get("name", "")
                    tool_input = tc.get("input", {})

                    # Notify the caller about the tool call
                    if self._status_callback:
                        self._status_callback(
                            "agent_tool_call",
                            {
                                "agent_id": agent_id,
                                "tool_name": tool_name,
                                "params": tool_input,
                            },
                        )

                    result_text = self._execute_tool(tool_name, tool_input)

                    all_tool_calls.append(
                        {
                            "name": tool_name,
                            "input": tool_input,
                            "result": result_text[:300],
                        }
                    )

                    # Notify the caller about the tool result
                    if self._status_callback:
                        self._status_callback(
                            "agent_tool_result",
                            {
                                "agent_id": agent_id,
                                "tool_name": tool_name,
                                "result": result_text[:500],
                                "status": "success",
                            },
                        )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.get("id", ""),
                            "content": result_text,
                        }
                    )

                messages.append(
                    {"role": "assistant", "content": response.get("content_blocks", [])}
                )
                messages.append({"role": "user", "content": tool_results})
                if _is_cancelled():
                    return _cancelled_result()
                continue

            # Handle max_tokens truncation — ask the model to summarise
            if stop_reason == "max_tokens":
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your response was truncated. Please provide a concise "
                            "summary of your findings so far."
                        ),
                    },
                )
                continue

            # Final response — the agent has finished
            content = response.get("content", "")

            # A required terminal tool was never called. Give the model one firm,
            # tool-restricted chance to submit before accepting a plain-text finish,
            # so prose or an early stop does not discard the whole turn.
            if terminal_tool and not forced_terminal:
                forced_terminal = True
                messages.append(
                    {"role": "assistant", "content": response.get("content_blocks", [])}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[SYSTEM] You have not called `{terminal_tool}` yet. Stop now and "
                            f"call `{terminal_tool}` exactly once to record your result. "
                            f"Respond with the tool call only."
                        ),
                    }
                )
                if terminal_only_tools:
                    tools = terminal_only_tools
                continue

            if self._status_callback:
                self._status_callback(
                    "agent_complete",
                    {
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "status": "completed",
                        "result": content[:500],
                        "input_tokens": total_input,
                        "output_tokens": total_output,
                    },
                )

            return {
                "content": content,
                "status": "completed",
                "input_tokens": total_input,
                "output_tokens": total_output,
                "tool_calls": all_tool_calls,
            }

        # Max iterations reached. If a terminal tool is still required, make one
        # final, tool-restricted attempt so heavy research does not discard the turn.
        if terminal_tool and not forced_terminal and not _is_cancelled():
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[SYSTEM] You have reached the research limit. Call `{terminal_tool}` "
                        f"now, exactly once, to record your result. Respond with the tool call only."
                    ),
                }
            )
            response = self._llm.invoke_model(
                messages,
                max_tokens=max_tokens,
                temperature=0.7,
                tools=terminal_only_tools or None,
                system=system,
                prompt_caching=prompt_caching,
            )
            usage = response.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)
            if response.get("stop_reason") == "tool_use":
                for tc in response.get("tool_use") or []:
                    if tc.get("name") == terminal_tool:
                        return _terminal_result(tc)

        # Max iterations reached without a final response
        if self._status_callback:
            self._status_callback(
                "agent_complete",
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "status": "completed",
                    "result": "Max iterations reached",
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                },
            )

        return {
            "content": f"Agent reached max iterations ({max_iterations}).",
            "status": "completed",
            "input_tokens": total_input,
            "output_tokens": total_output,
            "tool_calls": all_tool_calls,
        }

    def _build_system(self, agent_name: str, task: str, mode: str) -> str:
        """Build the agent's system prompt with environment context."""
        import platform
        import sys
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if sys.platform == "darwin":
            os_info = f"macOS ({platform.machine()})"
        elif sys.platform == "win32":
            os_info = f"Windows ({platform.machine()})"
        else:
            os_info = f"Linux ({platform.machine()})"

        base = (
            f"## Identity\n\n"
            f"You are a Spark sub-agent — an autonomous worker spawned by a parent "
            f"conversation to perform a specific task independently. Your name is "
            f"'{agent_name}'. You are NOT the main conversation assistant — you are "
            f"a focused worker that completes a task and returns results.\n\n"
            f"**Current time:** {now}\n"
            f"**Operating system:** {os_info}\n\n"
            f"## Your Assigned Task\n\n{task}\n\n"
            f"## Working Instructions\n\n"
        )

        if mode == "orchestrator":
            base += (
                "You are working in **orchestrator mode** — you have fresh context "
                "with only your task description. Complete it directly and efficiently.\n\n"
            )
        else:
            base += (
                "You are working in **chain mode** — you can see the parent conversation "
                "history for context. Focus on your assigned task, not on continuing the "
                "conversation.\n\n"
            )

        base += (
            "- Complete your task using the available tools, then provide a clear "
            "summary of your findings and results\n"
            "- Be thorough but concise — your output will be returned to the parent "
            "conversation\n"
            "- The current date/time is already provided above — do NOT call "
            "`get_current_datetime` unless you specifically need a different timezone\n"
            "- Focus exclusively on your assigned task — do not perform unrelated "
            "lookups or unnecessary steps\n"
            "- Use `get_tool_documentation(tool_name)` if you need help with any tool\n"
            "- You cannot spawn further sub-agents\n"
        )

        # Advertise enabled skills (global state; failure must never break the run)
        try:
            from spark.database import skills as skills_db
            from spark.skills.manager import get_skills_manager
            from spark.skills.prompt import build_skills_block

            block = build_skills_block(
                skills_db.resolve_enabled(self._db, get_skills_manager(), self._user_guid)
            )
            if block:
                base += "\n" + block + "\n"
        except Exception:  # noqa: BLE001
            logger.warning("Skills block unavailable", exc_info=True)

        return base

    def _get_tools(self) -> list[dict]:
        """Get available tools, excluding spawn_agent to prevent recursive spawning."""
        try:
            from spark.tools.registry import get_builtin_tools

            tools = get_builtin_tools(self._config)

            # Add MCP tools if the parent's MCP manager is available
            if self._mcp_manager and self._mcp_loop:
                import asyncio

                try:
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
                except Exception as e:
                    logger.warning("Agent failed to load MCP tools: %s", e)

            # Do NOT include spawn_agent — agents cannot spawn sub-agents
            return tools
        except Exception as e:
            logger.warning("Agent failed to load tools: %s", e)
            return []

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool, reusing the parent's tool infrastructure."""
        if tool_name in getattr(self, "_exclude_tools", frozenset()):
            return f"Tool '{tool_name}' is not available in this context."
        try:
            from spark.tools.registry import execute_builtin_tool, get_builtin_tools

            config = dict(self._config)

            # Inject memory index for memory-related tools
            try:
                from spark.index.memory_index import MemoryIndex

                config["_memory_index"] = MemoryIndex(self._db, self._user_guid)
            except Exception:
                pass

            # Check whether this is a builtin tool
            builtin_names = {t["name"] for t in get_builtin_tools(config)}

            if tool_name in builtin_names:
                result, is_error = execute_builtin_tool(tool_name, tool_input, config)
                return result

            # Fall back to MCP tools
            if self._mcp_manager and self._mcp_loop:
                import asyncio

                try:
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
                    return f"MCP tool error: {e}"

            return f"Tool '{tool_name}' not available."
        except Exception as e:
            return f"Tool error: {e}"
