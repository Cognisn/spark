"""Conversation Manager — core orchestration layer for Spark."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from spark.core.context_compaction import ContextCompactor
from spark.database.connection import DatabaseConnection
from spark.llm.base import LLMService
from spark.llm.context_limits import ContextLimitResolver
from spark.llm.manager import LLMManager

logger = logging.getLogger(__name__)

# Marker prefix for tool results stored as user messages
_TOOL_RESULTS_MARKER = "[TOOL_RESULTS]"


class ConversationManager:
    """Orchestrates conversation lifecycle, message handling, and tool integration.

    This is the central component tying together the database, LLM providers,
    tools, MCP servers, memory, and context compaction.
    """

    def __init__(
        self,
        db: DatabaseConnection,
        llm_manager: LLMManager,
        context_limits: ContextLimitResolver,
        *,
        global_instructions: str | None = None,
        max_tool_iterations: int = 25,
        max_tool_selections: int = 30,
        max_tool_result_tokens: int = 4000,
        rollup_threshold: float = 0.7,
        rollup_summary_ratio: float = 0.3,
        emergency_rollup_threshold: float = 0.95,
        compaction_model: str | None = None,
        mcp_manager: Any | None = None,
        user_guid: str = "default",
        mcp_loop: Any | None = None,
        tool_permission_callback: Callable | None = None,
        agent_model_callback: Callable | None = None,
        embedded_tools_config: dict[str, Any] | None = None,
        index_config: dict[str, Any] | None = None,
        prompt_caching: bool = True,
    ) -> None:
        self._db = db
        self._llm = llm_manager
        self._context_limits = context_limits
        self._global_instructions = global_instructions
        self._max_tool_iterations = max_tool_iterations
        self._max_tool_selections = max_tool_selections
        self._max_tool_result_tokens = max_tool_result_tokens
        self._mcp_manager = mcp_manager
        self._user_guid = user_guid
        self._mcp_loop = mcp_loop
        self._tool_permission_callback = tool_permission_callback
        self._agent_model_callback = agent_model_callback
        self._embedded_tools_config = embedded_tools_config or {}
        self._index_config = index_config or {}
        self._prompt_caching_enabled = prompt_caching
        self._in_tool_use_loop = False

        # Per-request state for agent tool execution
        self._current_conversation_id: int | None = None
        self._current_user_guid: str | None = None
        self._current_status_callback: Callable | None = None

        # Cache embedded tool definitions
        self._builtin_tools: list[dict[str, Any]] | None = None
        # Set of builtin tool names for fast lookup
        self._builtin_tool_names: set[str] | None = None

        # Per-conversation vector index cache
        self._vector_indices: dict[int, Any] = {}

        # Context compactor — uses active LLM service
        service = llm_manager.active_service
        if service:
            self._compactor = ContextCompactor(
                service,
                db,
                context_limits,
                threshold=rollup_threshold,
                emergency_threshold=emergency_rollup_threshold,
                summary_ratio=rollup_summary_ratio,
                locked_model=compaction_model,
            )
        else:
            self._compactor = None

    def _get_vector_index(self, conversation_id: int, user_guid: str) -> Any:
        """Get or create the vector index for a conversation."""
        if conversation_id not in self._vector_indices:
            try:
                from spark.index.vector_index import ConversationVectorIndex

                self._vector_indices[conversation_id] = ConversationVectorIndex(
                    self._db,
                    conversation_id,
                    user_guid,
                    self._index_config,
                )
            except Exception as e:
                logger.debug("Vector index not available: %s", e)
                return None
        return self._vector_indices.get(conversation_id)

    def _index_message(
        self, conversation_id: int, user_guid: str, msg_id: int, role: str, content: str
    ) -> None:
        """Index a message in the vector store (best-effort, non-blocking)."""
        idx = self._get_vector_index(conversation_id, user_guid)
        if idx is None:
            return
        try:
            idx.index_message(msg_id, role, content)
        except Exception as e:
            logger.debug("Failed to index message: %s", e)

    def _index_tool_call(
        self, conversation_id: int, user_guid: str, tool_name: str, tool_input: dict, msg_id: int
    ) -> None:
        """Index a tool call in the vector store."""
        idx = self._get_vector_index(conversation_id, user_guid)
        if idx is None:
            return
        try:
            idx.index_tool_call(tool_name, tool_input, msg_id)
        except Exception as e:
            logger.debug("Failed to index tool call: %s", e)

    def _index_tool_result(
        self,
        conversation_id: int,
        user_guid: str,
        tool_name: str,
        result: str,
        msg_id: int,
        *,
        is_error: bool = False,
    ) -> None:
        """Index a tool result in the vector store."""
        idx = self._get_vector_index(conversation_id, user_guid)
        if idx is None:
            return
        try:
            idx.index_tool_result(tool_name, result, msg_id, is_error=is_error)
        except Exception as e:
            logger.debug("Failed to index tool result: %s", e)

    def _retrieve_relevant_context(
        self, conversation_id: int, user_guid: str, query: str, conv: dict
    ) -> str | None:
        """Retrieve relevant context from the vector index for augmentation.

        Searches the current conversation AND any linked conversations.
        Only returns context from messages that have been compacted (rolled up),
        NOT from messages still in the active history — those are already sent
        to the LLM as part of the conversation and would waste tokens.
        """
        if not conv.get("rag_enabled", True):
            return None

        idx = self._get_vector_index(conversation_id, user_guid)
        if idx is None:
            return None

        top_k = conv.get("rag_top_k") or 5
        threshold = conv.get("rag_threshold") or 0.4

        try:
            # Get the content of active (non-rolled-up) messages to filter them out
            from spark.database import messages as msg_db

            active_msgs = msg_db.get_messages(self._db, conversation_id)
            active_content_hashes = set()
            for m in active_msgs:
                content = m.get("content", "")
                if content:
                    import hashlib

                    # Hash the content the same way context_index does
                    active_content_hashes.add(hashlib.sha256(content.encode()).hexdigest())

            # Build list of conversation IDs to search (current + linked)
            search_ids = [conversation_id]
            try:
                from spark.database import conversation_links

                linked = conversation_links.get_links(self._db, conversation_id, user_guid)
                for link in linked:
                    link_id = link.get("id")
                    if link_id:
                        search_ids.append(link_id)
                        self._get_vector_index(link_id, user_guid)
            except Exception as e:
                logger.debug("Failed to get linked conversations: %s", e)

            # Request extra results since we'll filter some out
            fetch_k = top_k * 3

            if len(search_ids) > 1:
                logger.info(
                    "RAG search across %d conversations (current + %d linked)",
                    len(search_ids),
                    len(search_ids) - 1,
                )
                results = idx.search_multi(query, search_ids, top_k=fetch_k, threshold=threshold)
            else:
                results = idx.search(query, top_k=fetch_k, threshold=threshold)

            if not results:
                return None

            # Filter out results whose content is already in active messages
            filtered = []
            for r in results:
                content = r.get("content_text", "")
                content_hash = r.get("content_hash", "")
                conv_id = r.get("conversation_id", conversation_id)

                # Always include results from linked conversations
                if conv_id != conversation_id:
                    filtered.append(r)
                    continue

                # For current conversation, skip if content is in active history
                if content_hash and content_hash in active_content_hashes:
                    continue
                if content:
                    import hashlib

                    h = hashlib.sha256(content.encode()).hexdigest()
                    if h in active_content_hashes:
                        continue

                filtered.append(r)

            filtered = filtered[:top_k]

            if not filtered:
                return None

            lines = ["## Relevant Context from History\n"]
            for r in filtered:
                element_type = r.get("element_type", "")
                content = r.get("content_text", "")
                conv_id = r.get("conversation_id", conversation_id)
                source = "(linked)" if conv_id != conversation_id else ""
                if content:
                    lines.append(f"- [{element_type}]{source} {content[:300]}")

            logger.info(
                "RAG retrieved %d relevant context items (filtered from %d)",
                len(filtered),
                len(results),
            )
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Failed to retrieve context: %s", e)
            return None

    def _get_builtin_tools(self) -> list[dict[str, Any]]:
        """Get built-in tool definitions (cached)."""
        if self._builtin_tools is None:
            from spark.tools.registry import get_builtin_tools

            self._builtin_tools = get_builtin_tools(self._embedded_tools_config)
            self._builtin_tool_names = {t["name"] for t in self._builtin_tools}
        return self._builtin_tools

    def _is_builtin_tool(self, tool_name: str) -> bool:
        """Check if a tool name belongs to the built-in tools."""
        if self._builtin_tool_names is None:
            self._get_builtin_tools()
        return tool_name in (self._builtin_tool_names or set())

    def _get_agent_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions for agent spawning when agents are enabled."""
        enabled = (
            self._embedded_tools_config.get("embedded_tools", {})
            .get("agents", {})
            .get("enabled", False)
        )
        if not enabled:
            return []

        return [
            {
                "name": "spawn_agent",
                "description": (
                    "Spawn a sub-agent to execute a task independently. The agent runs "
                    "with its own context and returns a result when finished. Use this to "
                    "delegate research, analysis, or multi-step tasks."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "A clear description of the task for the agent to perform.",
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "A short descriptive name for this agent (e.g. 'Researcher', 'Analyst').",
                        },
                        "model_id": {
                            "type": "string",
                            "description": (
                                "Optional model ID to use. If omitted, the current conversation's "
                                "model is used. Use list_provider_models to see available options."
                            ),
                        },
                        "model_justification": {
                            "type": "string",
                            "description": (
                                "When specifying a model_id, explain why this model was chosen "
                                "over others (e.g. 'Choosing a faster model for simple data "
                                "gathering' or 'Using the most capable model for complex analysis'). "
                                "This is shown to the user for approval."
                            ),
                        },
                    },
                    "required": ["task", "agent_name"],
                },
            },
            {
                "name": "list_provider_models",
                "description": (
                    "List all models available from the current LLM provider. "
                    "Useful for choosing a model when spawning an agent."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def _get_all_tools(self) -> list[dict[str, Any]]:
        """Get all tools: built-in + MCP server tools."""
        tools = list(self._get_builtin_tools())

        if self._mcp_manager:
            try:
                import asyncio

                # list_all_tools is async — dispatch on the persistent MCP loop
                if self._mcp_loop and self._mcp_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._mcp_manager.list_all_tools(), self._mcp_loop
                    )
                    mcp_tools = future.result(timeout=10)
                else:
                    # Fallback for cases without a persistent loop (e.g. tests)
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures

                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                mcp_tools = pool.submit(
                                    lambda: asyncio.run(self._mcp_manager.list_all_tools())
                                ).result(timeout=10)
                        else:
                            mcp_tools = loop.run_until_complete(self._mcp_manager.list_all_tools())
                    except RuntimeError:
                        mcp_tools = asyncio.run(self._mcp_manager.list_all_tools())

                for t in mcp_tools:
                    tools.append(
                        {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "inputSchema": t.get("inputSchema", {}),
                        }
                    )
                if mcp_tools:
                    logger.info(
                        "Loaded %d MCP tools from %d servers",
                        len(mcp_tools),
                        len(self._mcp_manager.servers),
                    )
            except Exception as e:
                logger.warning("Failed to load MCP tools: %s", e)

        # Agent tools (spawn_agent, list_provider_models)
        tools.extend(self._get_agent_tools())

        return tools

    # -- Conversation lifecycle -----------------------------------------------

    def create_conversation(
        self,
        name: str,
        model_id: str,
        user_guid: str,
        *,
        instructions: str | None = None,
        web_search_enabled: bool = False,
    ) -> int:
        """Create a new conversation. Returns conversation ID."""
        from spark.database import conversations

        cid = conversations.create_conversation(
            self._db,
            name,
            model_id,
            user_guid,
            instructions=instructions,
            web_search_enabled=web_search_enabled,
        )
        # Set the model on the LLM manager
        try:
            self._llm.set_model(model_id)
        except ValueError:
            logger.warning("Model %s not found in any provider", model_id)

        return cid

    def get_conversations(self, user_guid: str) -> list[dict]:
        """Get all active conversations."""
        from spark.database import conversations

        return conversations.get_active_conversations(self._db, user_guid)

    def get_conversation(self, conversation_id: int, user_guid: str) -> dict | None:
        """Get a single conversation."""
        from spark.database import conversations

        return conversations.get_conversation(self._db, conversation_id, user_guid)

    def delete_conversation(self, conversation_id: int, user_guid: str) -> None:
        """Soft-delete a conversation."""
        from spark.database import conversations

        conversations.delete_conversation(self._db, conversation_id, user_guid)

    # -- Message handling -----------------------------------------------------

    def get_messages(self, conversation_id: int) -> list[dict]:
        """Get active messages for a conversation."""
        from spark.database import messages

        return messages.get_messages(self._db, conversation_id)

    def send_message(
        self,
        conversation_id: int,
        user_message: str,
        user_guid: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        stream_callback: Callable | None = None,
        status_callback: Callable | None = None,
    ) -> dict[str, Any]:
        """Send a user message and get the assistant response.

        If tools is None, automatically loads embedded built-in tools.
        """
        from spark.database import conversations
        from spark.database import messages as msg_db

        conv = conversations.get_conversation(self._db, conversation_id, user_guid)
        if not conv:
            raise ValueError(f"Conversation {conversation_id} not found")

        model_id = conv.get("model_id", "")
        try:
            self._llm.set_model(model_id)
        except ValueError:
            pass

        # Auto-load embedded tools + MCP tools if none provided
        if tools is None:
            tools = self._get_all_tools()

        # Prompt inspection (if enabled)
        try:
            from spark.safety.inspector import PromptInspector

            inspector_enabled = self._embedded_tools_config.get("_prompt_inspection_enabled", False)
            if inspector_enabled:
                inspector_level = self._embedded_tools_config.get(
                    "_prompt_inspection_level", "standard"
                )
                inspector_action = self._embedded_tools_config.get(
                    "_prompt_inspection_action", "warn"
                )
                inspector = PromptInspector(
                    level=inspector_level, action=inspector_action, db=self._db
                )
                result = inspector.inspect(user_message, user_guid=user_guid)
                if not result.is_safe:
                    logger.warning(
                        "Prompt inspection flagged: %s (severity=%s, action=%s)",
                        result.explanation,
                        result.severity,
                        result.action,
                    )
                    if result.action == "block":
                        return {
                            "content": f"Message blocked by security inspection: {result.explanation}",
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                            "tool_calls": [],
                            "iterations": 0,
                            "blocked": True,
                        }
        except Exception as e:
            logger.debug("Prompt inspection error (non-fatal): %s", e)

        # Add user message
        token_count = self._llm.count_tokens(user_message)
        user_msg_id = msg_db.add_message(
            self._db, conversation_id, "user", user_message, token_count, user_guid
        )

        # Index user message in vector store
        self._index_message(conversation_id, user_guid, user_msg_id, "user", user_message)

        # Retrieve relevant context from vector index (respects conv settings)
        retrieved_context = self._retrieve_relevant_context(
            conversation_id, user_guid, user_message, conv
        )
        if retrieved_context:
            logger.info(
                "RAG context injected into system prompt (%d chars)", len(retrieved_context)
            )
        else:
            logger.debug("No RAG context retrieved for this message")

        # Build system instructions (with retrieved context if available)
        system = self._build_system_instructions(conv, retrieved_context=retrieved_context)
        logger.debug(
            "System prompt: %d chars, %d messages in history",
            len(system),
            len(self._get_messages_for_model(conversation_id, conv)),
        )

        # Tool use loop
        self._in_tool_use_loop = True
        total_usage = {"input_tokens": 0, "output_tokens": 0}
        all_tool_calls: list[dict] = []
        final_content = ""
        iterations = 0

        try:
            for iteration in range(self._max_tool_iterations):
                iterations = iteration + 1

                # Get messages for model (respects conv settings)
                history = self._get_messages_for_model(conversation_id, conv)

                # Invoke model (with error recovery)
                try:
                    # Resolve prompt caching: global default, overridden by per-conversation setting
                    use_caching = self._prompt_caching_enabled
                    if conv and conv.get("prompt_caching") is not None:
                        use_caching = bool(conv.get("prompt_caching", True))

                    response = self._llm.invoke_model(
                        history,
                        max_tokens=self._context_limits.get_max_output(model_id),
                        temperature=0.7,
                        tools=tools if tools else None,
                        system=system,
                        stream_callback=stream_callback,
                        prompt_caching=use_caching,
                    )
                except Exception as e:
                    error_msg = str(e)
                    if "rate" in error_msg.lower() or "429" in error_msg:
                        logger.warning("Rate limited, will retry: %s", error_msg[:200])
                        time.sleep(2)
                        continue
                    logger.error("LLM invocation failed: %s", error_msg[:300])
                    # Store error as assistant message so user sees it
                    error_content = (
                        f"I encountered an error communicating with the model: {error_msg}"
                    )
                    msg_db.add_message(
                        self._db,
                        conversation_id,
                        "assistant",
                        error_content,
                        self._llm.count_tokens(error_content),
                        user_guid,
                    )
                    return {
                        "content": error_content,
                        "usage": total_usage,
                        "tool_calls": all_tool_calls,
                        "iterations": iterations,
                        "error": True,
                    }

                # Accumulate usage (including cache stats if present)
                resp_usage = response.get("usage", {})
                total_usage["input_tokens"] += resp_usage.get("input_tokens", 0)
                total_usage["output_tokens"] += resp_usage.get("output_tokens", 0)
                if resp_usage.get("cache_read_input_tokens"):
                    total_usage["cache_read_input_tokens"] = (
                        total_usage.get("cache_read_input_tokens", 0)
                        + resp_usage["cache_read_input_tokens"]
                    )
                if resp_usage.get("cache_creation_input_tokens"):
                    total_usage["cache_creation_input_tokens"] = (
                        total_usage.get("cache_creation_input_tokens", 0)
                        + resp_usage["cache_creation_input_tokens"]
                    )

                stop_reason = response.get("stop_reason", "end_turn")

                if stop_reason == "tool_use" and response.get("tool_use"):
                    # Execute tools and continue loop
                    tool_results = self._execute_tools(
                        conversation_id,
                        response["tool_use"],
                        user_guid,
                        status_callback=status_callback,
                    )
                    all_tool_calls.extend(response["tool_use"])

                    # Store assistant message with tool calls
                    assistant_content = response.get("content_blocks", [])
                    assistant_text = json.dumps(assistant_content) if assistant_content else ""
                    assistant_tokens = self._llm.count_tokens(assistant_text)
                    asst_msg_id = msg_db.add_message(
                        self._db,
                        conversation_id,
                        "assistant",
                        assistant_text,
                        assistant_tokens,
                        user_guid,
                    )

                    # Index tool calls and results in vector store
                    for tc in response["tool_use"]:
                        self._index_tool_call(
                            conversation_id,
                            user_guid,
                            tc.get("name", ""),
                            tc.get("input", {}),
                            asst_msg_id,
                        )
                    for tr in tool_results:
                        if tr.get("type") == "tool_result":
                            self._index_tool_result(
                                conversation_id,
                                user_guid,
                                tr.get("tool_use_id", ""),
                                tr.get("content", ""),
                                asst_msg_id,
                                is_error=tr.get("is_error", False),
                            )

                    # Store tool results as user message
                    results_text = json.dumps(tool_results)
                    results_tokens = self._llm.count_tokens(results_text)
                    msg_db.add_message(
                        self._db,
                        conversation_id,
                        "user",
                        f"{_TOOL_RESULTS_MARKER}{results_text}",
                        results_tokens,
                        user_guid,
                    )

                    # Update token usage
                    conversations.update_token_usage(
                        self._db,
                        conversation_id,
                        model_id,
                        resp_usage.get("input_tokens", 0),
                        resp_usage.get("output_tokens", 0),
                        user_guid,
                    )

                    if status_callback:
                        status_callback("tool_iteration_complete", {"iteration": iterations})

                    continue

                # Final text response
                final_content = response.get("content", "")
                final_tokens = self._llm.count_tokens(final_content)
                final_msg_id = msg_db.add_message(
                    self._db,
                    conversation_id,
                    "assistant",
                    final_content,
                    final_tokens,
                    user_guid,
                )

                # Index assistant response in vector store
                self._index_message(
                    conversation_id, user_guid, final_msg_id, "assistant", final_content
                )

                conversations.update_token_usage(
                    self._db,
                    conversation_id,
                    model_id,
                    resp_usage.get("input_tokens", 0),
                    resp_usage.get("output_tokens", 0),
                    user_guid,
                )
                break

        finally:
            self._in_tool_use_loop = False

        # Check for deferred compaction
        self._check_compaction(conversation_id, model_id, status_callback)

        return {
            "content": final_content,
            "usage": total_usage,
            "tool_calls": all_tool_calls,
            "iterations": iterations,
        }

    # -- System instructions --------------------------------------------------

    def _build_conversation_tool_context(self) -> str:
        """Build additional system prompt context for enabled tools."""
        embedded = self._embedded_tools_config.get("embedded_tools", {})
        parts: list[str] = []

        # Filesystem allowed paths
        fs_config = embedded.get("filesystem", {})
        allowed_paths = fs_config.get("allowed_paths", [])
        if isinstance(allowed_paths, str):
            allowed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]
        if fs_config.get("enabled", True) and allowed_paths:
            path_list = ", ".join(f"`{p}`" for p in allowed_paths)
            parts.append(
                f"**Filesystem access:** You can read"
                + (" and write" if fs_config.get("mode") == "read_write" else "")
                + f" files within these paths: {path_list}\n"
            )

        # System commands
        cmd_config = embedded.get("system_commands", {})
        if cmd_config.get("enabled", False):
            import platform
            import sys

            if sys.platform == "darwin":
                os_desc = "macOS with zsh shell"
            elif sys.platform == "win32":
                os_desc = "Windows with PowerShell/cmd"
            else:
                os_desc = "Linux with bash shell"

            parts.append(
                f"**System commands:** You can execute shell commands on the host system "
                f"({os_desc}, {platform.machine()}). Use `run_command` to run CLI tools "
                f"like git, docker, aws, curl, etc. Use `get_tool_documentation('run_command')` "
                f"for the full usage guide. Commands require user approval by default.\n"
            )

        # Email
        email_config = embedded.get("email", {})
        if email_config.get("enabled", False) and email_config.get("host"):
            sender = email_config.get("sender", "")
            if sender:
                parts.append(
                    f"**Email:** You can send emails via `send_email` (sender: {sender}) "
                    f"or save drafts via `draft_email`.\n"
                )

        # Agents
        agent_config = embedded.get("agents", {})
        if agent_config.get("enabled", False):
            mode = agent_config.get("default_mode", "orchestrator")
            mode_desc = (
                "orchestrator-workers (agents get fresh context with just the task)"
                if mode == "orchestrator"
                else "chain (agents see the full conversation context)"
            )
            model_selection = agent_config.get("model_selection", "same")

            agent_desc = (
                f"**Agent spawning:** You can spawn independent sub-agents via `spawn_agent` "
                f"to work on tasks in parallel. Current mode: {mode_desc}.\n\n"
                f"**When to use agents:** You should proactively consider spawning agents when:\n"
                f"- The user's request involves multiple independent tasks (e.g. research + analysis + writing)\n"
                f"- Tasks can be done in parallel (e.g. gathering data from different sources)\n"
                f"- A sub-task is complex enough to benefit from focused, independent work\n"
                f"- You want to delegate research while continuing to interact with the user\n\n"
                f"**When NOT to use agents:** Simple single-step tasks, quick tool calls, "
                f"or tasks that require back-and-forth with the user.\n\n"
                f"Agents have access to all enabled tools. "
                f"Use `get_tool_documentation('spawn_agent')` for the full guide.\n"
            )

            if model_selection in ("auto_select", "auto_select_approved"):
                approval_note = (
                    "The user will be asked to approve your model choice before the agent starts."
                    if model_selection == "auto_select"
                    else "Your model choice will be used directly without user confirmation."
                )
                agent_desc += (
                    f"**Agent model selection:** You can choose the model for each agent. "
                    f"Use `list_provider_models` to see available models from the current provider, "
                    f"then pass your chosen model_id and model_justification to `spawn_agent`. "
                    f"{approval_note} Consider using:\n"
                    f"- Faster/cheaper models for simple data gathering tasks\n"
                    f"- More capable models for complex analysis or reasoning\n"
                )
                # List models for quick reference
                try:
                    provider_name = self._llm.active_provider
                    if provider_name and provider_name in self._llm.providers:
                        models = self._llm.providers[provider_name].list_available_models()
                        if models:
                            model_lines = []
                            for m in models[:10]:
                                ctx = m.get("context_length", "?")
                                if isinstance(ctx, int) and ctx >= 1000:
                                    ctx = f"{ctx // 1000}K"
                                model_lines.append(
                                    f"  - `{m['id']}` ({m.get('name', '')}, {ctx} ctx)"
                                )
                            agent_desc += "Available models:\n" + "\n".join(model_lines) + "\n"
                except Exception:
                    pass
            else:
                agent_desc += "Agents will use the same model as this conversation.\n"

            parts.append(agent_desc)

        if parts:
            return "\n".join(parts) + "\n"
        return ""

    def _build_system_instructions(
        self, conv: dict, *, retrieved_context: str | None = None
    ) -> str:
        """Assemble system instructions from all sources."""
        parts: list[str] = []

        # 1. Embedded identity and capabilities
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts.append(
            f"You are Spark, a secure personal AI research assistant.\n"
            f"Current date/time: {now}\n\n"
            f"## Your Capabilities\n\n"
            f"You have access to tools that you should actively use when appropriate:\n\n"
            f"**Memory tools:** You can store and retrieve persistent information across conversations.\n"
            f"- `store_memory` — Save facts, preferences, projects, instructions, or relationships the user shares.\n"
            f"- `query_memory` — Search stored memories by semantic relevance. Use this to recall information from past conversations.\n"
            f"- `list_memories` — List all stored memories, optionally filtered by category.\n"
            f"- `delete_memory` — Remove a specific memory by ID.\n\n"
            f"**When the user tells you something about themselves, their work, or their preferences, proactively use `store_memory` to remember it.**\n"
            f"**When the user asks about something that might have been discussed before, use `query_memory` to check.**\n\n"
            f"**Other tools:** You may also have access to filesystem, document, web search, email, and other tools. "
            f"Use them when the user's request requires reading files, searching the web, or performing other actions.\n\n"
            + self._build_conversation_tool_context()
            + f"**Tool documentation:** Use the `get_tool_documentation` tool to retrieve detailed usage instructions, "
            f"parameter references, examples, and best practices for any tool before using it. "
            f"Pass `_index` as the tool name to see all available documentation.\n\n"
            f"**Linked conversations:** If this conversation is linked to others, relevant context from those conversations "
            f"is automatically provided below. You can reference this context in your responses."
        )

        # 2. Global instructions
        if self._global_instructions:
            parts.append(self._global_instructions)

        # 3. Conversation-specific instructions
        conv_instructions = conv.get("instructions")
        if conv_instructions:
            parts.append(conv_instructions)

        # 4. Retrieved context from vector index and linked conversations
        if retrieved_context:
            parts.append(retrieved_context)

        return "\n\n".join(parts)

    # -- Message formatting ---------------------------------------------------

    def _get_messages_for_model(
        self, conversation_id: int, conv: dict | None = None
    ) -> list[dict[str, Any]]:
        """Format stored messages for the LLM API.

        Respects per-conversation settings:
        - max_history_messages: limit to last N messages (None = all)
        - include_tool_results: whether to include tool result messages
        """
        from spark.database import messages

        raw = messages.get_messages(self._db, conversation_id)
        include_tools = True
        max_msgs = None

        if conv:
            include_tools = conv.get("include_tool_results", 1) != 0
            max_msgs = conv.get("max_history_messages")

        formatted: list[dict[str, Any]] = []

        for msg in raw:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # Try to parse JSON content blocks (tool calls, tool results)
            if content.startswith(_TOOL_RESULTS_MARKER):
                if not include_tools:
                    continue
                json_str = content[len(_TOOL_RESULTS_MARKER) :]
                try:
                    blocks = json.loads(json_str)
                    formatted.append({"role": role, "content": blocks})
                    continue
                except json.JSONDecodeError:
                    pass

            if content.startswith("["):
                try:
                    blocks = json.loads(content)
                    if isinstance(blocks, list):
                        formatted.append({"role": role, "content": blocks})
                        continue
                except json.JSONDecodeError:
                    pass

            formatted.append({"role": role, "content": content})

        # Limit to last N messages if configured
        if max_msgs and len(formatted) > max_msgs:
            formatted = formatted[-max_msgs:]

        return formatted

    # -- Tool execution -------------------------------------------------------

    def _execute_single_tool(
        self,
        tool_call: dict,
        conversation_id: int,
        user_guid: str,
        *,
        status_callback: Callable | None = None,
    ) -> dict:
        """Execute a single tool call and return the tool_result dict.

        Extracted from _execute_tools so that agent calls can be dispatched
        to a thread pool for parallel execution.
        """
        from spark.database import mcp_ops, tool_permissions

        tool_name = tool_call.get("name", "")
        tool_id = tool_call.get("id", "")
        tool_input = tool_call.get("input", {})

        if status_callback:
            status_callback(
                "tool_call",
                {
                    "tool_use_id": tool_id,
                    "tool_name": tool_name,
                    "params": tool_input,
                },
            )

        # Check permissions (conversation-level, then global)
        permission = tool_permissions.is_tool_allowed(
            self._db, conversation_id, tool_name, user_guid=user_guid
        )

        # System commands: always prompt when require_approval is on,
        # unless running without a callback (autonomous actions).
        if (
            tool_name == "run_command"
            and self._tool_permission_callback
            and self._embedded_tools_config.get("embedded_tools", {})
            .get("system_commands", {})
            .get("require_approval", True)
        ):
            permission = None  # Force re-prompt

        # Email send_email: always prompt when require_approval is on,
        # unless running without a callback (autonomous actions).
        if (
            tool_name == "send_email"
            and self._tool_permission_callback
            and self._embedded_tools_config.get("embedded_tools", {})
            .get("email", {})
            .get("require_approval", True)
        ):
            permission = None  # Force re-prompt

        if permission is None:
            # First use — prompt user
            if self._tool_permission_callback:
                decision = self._tool_permission_callback(tool_name, tool_input)
                if decision in ("allowed", "allowed_global", "once"):
                    if decision == "allowed":
                        # Approve this tool and category siblings for this conversation
                        for sibling in _get_tool_category_siblings(tool_name):
                            tool_permissions.set_tool_permission(
                                self._db, conversation_id, sibling, "allowed", user_guid
                            )
                    elif decision == "allowed_global":
                        # Approve this tool and category siblings globally
                        for sibling in _get_tool_category_siblings(tool_name):
                            tool_permissions.set_global_tool_permission(
                                self._db, user_guid, sibling, "allowed"
                            )
                else:
                    tool_permissions.set_tool_permission(
                        self._db, conversation_id, tool_name, "denied", user_guid
                    )
                    return _tool_result(tool_id, "Tool execution denied by user.", is_error=True)
            # No callback — auto-allow (for testing or auto-approve mode)
        elif permission is False:
            return _tool_result(tool_id, "Tool execution denied.", is_error=True)

        # Execute
        logger.info(
            "Tool call: %s (id=%s) params=%s", tool_name, tool_id, json.dumps(tool_input)[:200]
        )
        start_time = time.monotonic()
        try:
            result_text, is_error = self._call_tool(tool_name, tool_input)
        except Exception as e:
            result_text = f"Tool execution error: {e}"
            is_error = True

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        if is_error:
            logger.warning("Tool %s failed (%dms): %s", tool_name, elapsed_ms, result_text[:200])
        else:
            logger.info("Tool %s completed (%dms): %s", tool_name, elapsed_ms, result_text[:100])

        # Truncate large results
        if len(result_text) > self._max_tool_result_tokens * 4:
            result_text = result_text[: self._max_tool_result_tokens * 4] + "\n... [truncated]"

        # Record transaction
        mcp_ops.record_transaction(
            self._db,
            conversation_id,
            tool_name,
            json.dumps(tool_input),
            result_text,
            user_guid,
            is_error=is_error,
            execution_time_ms=elapsed_ms,
        )

        if status_callback:
            status_callback(
                "tool_result",
                {
                    "tool_use_id": tool_id,
                    "tool_name": tool_name,
                    "result": result_text[:500],
                    "status": "error" if is_error else "success",
                },
            )

        return _tool_result(tool_id, result_text, is_error=is_error)

    def _execute_tools(
        self,
        conversation_id: int,
        tool_calls: list[dict],
        user_guid: str,
        *,
        status_callback: Callable | None = None,
    ) -> list[dict]:
        """Execute a batch of tool calls and return results.

        Non-agent tools are processed sequentially (preserving existing
        behaviour).  Multiple ``spawn_agent`` calls are dispatched in
        parallel via a thread pool so that independent agents can run
        concurrently.
        """
        # Store per-request context so _call_tool can access it for agent tools
        self._current_conversation_id = conversation_id
        self._current_user_guid = user_guid
        self._current_status_callback = status_callback

        # Separate agent calls from other tool calls, preserving order.
        non_agent_calls: list[tuple[int, dict]] = []
        agent_calls: list[tuple[int, dict]] = []

        for idx, tc in enumerate(tool_calls):
            if tc.get("name") == "spawn_agent":
                agent_calls.append((idx, tc))
            else:
                non_agent_calls.append((idx, tc))

        # Dict to collect results keyed by original index for correct ordering.
        result_map: dict[int, dict] = {}

        # 1. Execute non-agent tools sequentially.
        for idx, tc in non_agent_calls:
            result_map[idx] = self._execute_single_tool(
                tc, conversation_id, user_guid, status_callback=status_callback
            )

        # 2. Execute agent tools — in parallel when there are multiple,
        # but only if model auto-select with approval is NOT active (the modal
        # requires sequential execution). "auto_select_approved" trusts the
        # LLM's model choice without a modal, allowing parallel execution.
        embedded = self._embedded_tools_config.get("embedded_tools", {})
        agent_config = embedded.get("agents", {})
        model_selection = agent_config.get("model_selection", "same")
        needs_sequential = model_selection == "auto_select"

        if len(agent_calls) > 1 and not needs_sequential:
            import concurrent.futures

            logger.info("Dispatching %d spawn_agent calls in parallel", len(agent_calls))
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(agent_calls)) as pool:
                future_to_idx: dict[concurrent.futures.Future, int] = {}
                for idx, tc in agent_calls:
                    future = pool.submit(
                        self._execute_single_tool,
                        tc,
                        conversation_id,
                        user_guid,
                        status_callback=status_callback,
                    )
                    future_to_idx[future] = idx

                for future in concurrent.futures.as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        result_map[idx] = future.result()
                    except Exception as e:
                        tool_id = agent_calls[0][1].get("id", "")
                        for ai, atc in agent_calls:
                            if ai == idx:
                                tool_id = atc.get("id", "")
                                break
                        result_map[idx] = _tool_result(
                            tool_id, f"Agent execution error: {e}", is_error=True
                        )
        elif len(agent_calls) == 1:
            idx, tc = agent_calls[0]
            result_map[idx] = self._execute_single_tool(
                tc, conversation_id, user_guid, status_callback=status_callback
            )

        # 3. Reassemble results in the original tool_calls order.
        return [result_map[i] for i in range(len(tool_calls))]

    def _get_memory_index_for_tools(self) -> Any:
        """Get or create a MemoryIndex for tool use."""
        if not hasattr(self, "_memory_index_instance"):
            try:
                from spark.index.memory_index import MemoryIndex

                self._memory_index_instance = MemoryIndex(self._db, self._user_guid)
            except Exception:
                self._memory_index_instance = None
        return self._memory_index_instance

    def _get_llm_service_for_model(self, model_id: str) -> Any:
        """Get a dedicated LLM service instance for a specific model.

        Creates a fresh provider instance to avoid conflicts with concurrent
        conversations or agents that use different models on the same provider.
        """
        # Find which provider type owns this model
        for name, provider in self._llm.providers.items():
            try:
                models = provider.list_available_models()
                if any(m.get("id") == model_id for m in models):
                    # Create a fresh instance of the same provider type
                    fresh = self._clone_provider(name, provider)
                    if fresh:
                        fresh.set_model(model_id)
                        return fresh
                    # Fallback: use the shared instance (less safe but functional)
                    provider.set_model(model_id)
                    return provider
            except Exception:
                continue
        return None

    def _clone_provider(self, name: str, provider: Any) -> Any:
        """Create a fresh instance of an LLM provider with the same config."""
        try:
            provider_type = type(provider)
            if name == "Anthropic" or "anthropic" in str(provider_type).lower():
                return provider_type(api_key=getattr(provider, "_api_key", ""))
            elif name == "Ollama" or "ollama" in str(provider_type).lower():
                return provider_type(
                    base_url=getattr(provider, "_base_url", "http://localhost:11434")
                )
            elif name == "Google Gemini" or "gemini" in str(provider_type).lower():
                return provider_type(api_key=getattr(provider, "_api_key", ""))
            elif name == "X.AI" or "xai" in str(provider_type).lower():
                return provider_type(api_key=getattr(provider, "_api_key", ""))
            elif name == "AWS Bedrock" or "bedrock" in str(provider_type).lower():
                return provider_type(
                    region=getattr(provider, "_region", "us-east-1"),
                    profile=getattr(provider, "_profile", None),
                    access_key=getattr(provider, "_access_key", None),
                    secret_key=getattr(provider, "_secret_key", None),
                    session_token=getattr(provider, "_session_token", None),
                )
        except Exception as e:
            logger.debug("Could not clone provider %s: %s", name, e)
        return None

    def _list_provider_models(self) -> tuple[str, bool]:
        """List models from the conversation's current LLM provider."""
        try:
            provider_name = self._llm.active_provider
            if not provider_name or provider_name not in self._llm.providers:
                return "No active provider.", True
            models = self._llm.providers[provider_name].list_available_models()
            if not models:
                return f"No models available from {provider_name}.", False
            lines = [f"Available models from {provider_name}:\n"]
            for m in models:
                ctx = m.get("context_length", "?")
                tools_support = "Yes" if m.get("supports_tools") else "No"
                lines.append(
                    f"- {m['id']} ({m.get('name', '')}) — context: {ctx}, tools: {tools_support}"
                )
            return "\n".join(lines), False
        except Exception as e:
            return f"Error listing models: {e}", True

    def _execute_agent_spawn(
        self, tool_input: dict, conversation_id: int, user_guid: str
    ) -> tuple[str, bool]:
        """Spawn a sub-agent to execute a task."""
        import uuid

        from spark.core.agent_executor import AgentExecutor
        from spark.database import agents as agent_db

        task = tool_input.get("task", "").strip()
        agent_name = tool_input.get("agent_name", "Agent").strip()
        model_id = tool_input.get("model_id", "")

        if not task:
            return "Error: task description is required.", True

        # Determine model — fall back to the current conversation's model
        if not model_id:
            try:
                from spark.database import conversations

                conv = conversations.get_conversation(self._db, conversation_id, user_guid)
                if conv:
                    model_id = conv.get("model_id", "")
            except Exception:
                pass

        # Determine mode and model selection policy from settings
        embedded = self._embedded_tools_config.get("embedded_tools", {})
        agent_config = embedded.get("agents", {})
        mode = agent_config.get("default_mode", "orchestrator")
        max_iterations = agent_config.get("max_iterations", 15)

        # Check per-conversation override, then fall back to global config
        model_selection = agent_config.get("model_selection", "same")
        try:
            from spark.database import conversations as conv_db

            conv_row = conv_db.get_conversation(self._db, conversation_id, user_guid)
            per_conv = (conv_row or {}).get("agent_model_selection")
            if per_conv:
                model_selection = per_conv
        except Exception:
            pass

        # When auto_select is enabled and the LLM chose a model, request user
        # approval before execution.  The callback blocks this thread until the
        # user responds (mirroring the permission_request pattern).
        if model_selection == "auto_select" and model_id and self._agent_model_callback:
            provider_name = self._llm.active_provider
            available_models: list[dict] = []
            if provider_name and provider_name in self._llm.providers:
                available_models = self._llm.providers[provider_name].list_available_models()

            model_justification = tool_input.get("model_justification", "")
            approved_model = self._agent_model_callback(
                agent_name,
                task,
                model_id,
                [
                    {
                        "id": m["id"],
                        "name": m.get("name", m["id"]),
                        "context_length": m.get("context_length", 0),
                    }
                    for m in available_models
                ],
                model_justification,
            )
            if approved_model:
                model_id = approved_model

        agent_id = str(uuid.uuid4())[:12]

        # Record in database
        agent_db.create_agent_run(
            self._db,
            agent_id,
            conversation_id,
            agent_name,
            task,
            mode,
            model_id,
            user_guid,
        )

        # Get parent messages if chain mode
        parent_messages = None
        if mode == "chain":
            parent_messages = self._get_messages_for_model(conversation_id)

        # Create and run agent — get a dedicated LLM service for the model
        # to avoid conflicts with concurrent conversations using different providers.
        try:
            agent_llm = self._get_llm_service_for_model(model_id)
            if not agent_llm:
                agent_db.complete_agent_run(
                    self._db,
                    agent_id,
                    status="failed",
                    result_text=f"Could not find provider for model '{model_id}'",
                )
                return f"Agent failed: no provider found for model '{model_id}'.", True

            executor = AgentExecutor(
                agent_llm,
                self._db,
                self._embedded_tools_config,
                mcp_manager=self._mcp_manager,
                mcp_loop=getattr(self, "_mcp_loop", None),
                user_guid=user_guid,
                tool_permission_callback=self._tool_permission_callback,
                status_callback=self._current_status_callback,
            )

            result = executor.execute(
                agent_id,
                agent_name,
                task,
                model_id,
                mode=mode,
                parent_messages=parent_messages,
                max_iterations=max_iterations,
            )

            # Record completion
            agent_db.complete_agent_run(
                self._db,
                agent_id,
                status="completed",
                result_text=result.get("content", ""),
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                tool_calls_json=json.dumps(result.get("tool_calls", [])),
            )

            return result.get("content", "Agent completed with no output."), False

        except Exception as e:
            logger.error("Agent '%s' failed: %s", agent_name, e, exc_info=True)
            agent_db.complete_agent_run(
                self._db,
                agent_id,
                status="failed",
                result_text=str(e),
            )
            return f"Agent '{agent_name}' failed: {e}", True

    def _call_tool(self, tool_name: str, tool_input: dict) -> tuple[str, bool]:
        """Execute a single tool. Returns (result_text, is_error)."""
        # Agent tools — these need conversation context stored on the instance
        if tool_name == "spawn_agent":
            cid = self._current_conversation_id
            uid = self._current_user_guid
            if cid is None or uid is None:
                return "Error: no active conversation context for agent spawn.", True
            return self._execute_agent_spawn(tool_input, cid, uid)
        if tool_name == "list_provider_models":
            return self._list_provider_models()

        # Try built-in tools first
        if self._is_builtin_tool(tool_name):
            from spark.tools.registry import execute_builtin_tool

            # Inject memory index for memory tools
            config = dict(self._embedded_tools_config)
            config["_memory_index"] = self._get_memory_index_for_tools()
            return execute_builtin_tool(tool_name, tool_input, config)

        # Try MCP manager (async — dispatch on the persistent MCP event loop)
        if self._mcp_manager:
            try:
                import asyncio

                async def _call() -> dict:
                    return await self._mcp_manager.call_tool(tool_name, tool_input)

                if self._mcp_loop and self._mcp_loop.is_running():
                    # Dispatch onto the persistent MCP loop where the sessions live.
                    # This is critical: MCP stdio transports are bound to the loop
                    # they were created on — calling from a different loop will fail.
                    future = asyncio.run_coroutine_threadsafe(_call(), self._mcp_loop)
                    result = future.result(timeout=60)
                else:
                    # Fallback for cases without a persistent loop (e.g. tests)
                    result = asyncio.run(_call())

                # Extract text from result content
                content = result.get("content", [])
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        text_parts.append(item)
                result_text = "\n".join(text_parts) if text_parts else str(result)
                is_error = result.get("isError", False)
                return (result_text, is_error)
            except Exception as e:
                logger.warning("MCP tool %s execution failed: %s", tool_name, e)
                return (f"MCP tool error: {e}", True)

        return (f"Tool '{tool_name}' is not available.", True)

    # -- Context compaction ---------------------------------------------------

    def _check_compaction(
        self,
        conversation_id: int,
        model_id: str,
        status_callback: Callable | None = None,
    ) -> None:
        """Check and perform context compaction if needed."""
        if self._compactor:
            self._compactor.check_and_compact(
                conversation_id,
                model_id,
                in_tool_use_loop=self._in_tool_use_loop,
                status_callback=status_callback,
            )


_TOOL_CATEGORIES: dict[str, list[str]] = {
    "filesystem": [
        "read_file",
        "write_file",
        "list_directory",
        "search_files",
        "get_file_info",
        "find_in_file",
        "get_directory_tree",
    ],
    "documents": [
        "read_word",
        "read_excel",
        "read_pdf",
        "read_powerpoint",
        "create_word",
        "create_excel",
        "create_powerpoint",
        "create_pdf",
    ],
    "archives": ["list_archive", "extract_archive"],
    "web": ["web_search", "web_fetch"],
    "system_commands": ["run_command"],
    "memory": ["store_memory", "query_memory", "list_memories", "delete_memory"],
    "email": ["send_email", "draft_email"],
    "core": ["get_current_datetime", "get_tool_documentation"],
    "agents": ["spawn_agent", "list_provider_models"],
}


def _get_tool_category_siblings(tool_name: str) -> list[str]:
    """Return all tool names in the same category as the given tool.

    When a user approves a tool with 'Always Allow', all tools in the same
    category are also approved (e.g. approving web_search also approves web_fetch).
    """
    for _category, tools in _TOOL_CATEGORIES.items():
        if tool_name in tools:
            return tools
    return [tool_name]


def _tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict:
    """Build a standard tool_result block."""
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        result["is_error"] = True
    return result
