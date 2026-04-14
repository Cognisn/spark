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
        self._embedded_tools_config = embedded_tools_config or {}
        self._index_config = index_config or {}
        self._prompt_caching_enabled = prompt_caching
        self._in_tool_use_loop = False

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

    def _execute_tools(
        self,
        conversation_id: int,
        tool_calls: list[dict],
        user_guid: str,
        *,
        status_callback: Callable | None = None,
    ) -> list[dict]:
        """Execute a batch of tool calls and return results."""
        from spark.database import mcp_ops, tool_permissions

        results: list[dict] = []

        for tool_call in tool_calls:
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
                        results.append(
                            _tool_result(tool_id, "Tool execution denied by user.", is_error=True)
                        )
                        continue
                # No callback — auto-allow (for testing or auto-approve mode)
            elif permission is False:
                results.append(_tool_result(tool_id, "Tool execution denied.", is_error=True))
                continue

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
                logger.warning(
                    "Tool %s failed (%dms): %s", tool_name, elapsed_ms, result_text[:200]
                )
            else:
                logger.info(
                    "Tool %s completed (%dms): %s", tool_name, elapsed_ms, result_text[:100]
                )

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

            results.append(_tool_result(tool_id, result_text, is_error=is_error))

        return results

    def _get_memory_index_for_tools(self) -> Any:
        """Get or create a MemoryIndex for tool use."""
        if not hasattr(self, "_memory_index_instance"):
            try:
                from spark.index.memory_index import MemoryIndex

                self._memory_index_instance = MemoryIndex(self._db, self._user_guid)
            except Exception:
                self._memory_index_instance = None
        return self._memory_index_instance

    def _call_tool(self, tool_name: str, tool_input: dict) -> tuple[str, bool]:
        """Execute a single tool. Returns (result_text, is_error)."""
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
