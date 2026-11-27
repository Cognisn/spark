"""Conversation Manager — core orchestration layer for Spark."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from spark.connections.refs import ModelRef
from spark.connections.runtime import ModelSource, UserScope
from spark.connections.usage import Attribution
from spark.core.cancellation import CancellationToken
from spark.core.context_compaction import CompactionSettings, ContextCompactor, ordered_for_model
from spark.core.reasoning import response_reasoning
from spark.database.scope import NotOwned
from spark.db.bridge import SyncDatabase
from spark.safety.untrusted import (
    UNTRUSTED_CONTENT_INSTRUCTIONS,
    ResultPolicy,
    guard_tool_result,
    new_turn_id,
)
from spark.tools.registry import TOOL_CATEGORIES as _REGISTRY_CATEGORIES
from spark.tools.registry import Permissions
from spark.usage import budgets
from spark.usage.meter import meter

logger = logging.getLogger(__name__)

# Tools over a person's own memories and knowledge graphs: never offered or run in a
# shared conversation, whose every member reads the answer (CS28, CS31).
PRIVATE_CATEGORIES = ("memory", "knowledge")
PRIVATE_TOOLS = frozenset(name for c in PRIVATE_CATEGORIES for name in _REGISTRY_CATEGORIES[c])


def _without_private(permissions: Permissions) -> Permissions:
    """A user's permissions without the memory and knowledge categories (a server user's
    are a set)."""
    from collections.abc import Set

    from spark.tools.registry import permission_name

    if isinstance(permissions, Set):
        return frozenset(permissions) - {permission_name(c) for c in PRIVATE_CATEGORIES}
    return permissions


class ConnectionNotAssigned(PermissionError):
    """A server user's turn on a connection that is not (or no longer) assigned to them."""


class _CatalogueLimits:
    """Context windows and output limits from the connections' catalogue (for compaction)."""

    def __init__(self, models: ModelSource) -> None:
        self._models = models

    def get_context_window(self, model_id: str) -> int:
        return self._models.limits(model_id)[0]

    def get_max_output(self, model_id: str) -> int:
        return self._models.limits(model_id)[1]


# Marker prefix for tool results stored as user messages
_TOOL_RESULTS_MARKER = "[TOOL_RESULTS]"

# Instance settings a server reads at each turn, so a change applies without a restart
_ROLLUP_THRESHOLD = "conversation.rollup_threshold"
_ROLLUP_SUMMARY_RATIO = "conversation.rollup_summary_ratio"
_EMERGENCY_ROLLUP_THRESHOLD = "conversation.emergency_rollup_threshold"
_PROMPT_CACHING = "conversation.prompt_caching"


def _live_setting(key: str, fallback: Any) -> Any:
    """A server's current instance setting ``key`` (the administrator's, kept current by
    the settings listener), or ``fallback`` in personal mode or when it cannot be read."""
    from spark import mode

    try:
        if not mode.is_server():
            return fallback
        from spark.instance_settings import accessor

        value = accessor.get(key)
    except Exception:  # noqa: BLE001 - an unreadable setting keeps the start-up value
        return fallback
    return fallback if value is None else value


# The instance's "Auto-approve Tools" switch, read at each tool permission check
_AUTO_APPROVE_TOOLS = "tool_permissions.auto_approve"


def _auto_approve_tools() -> bool:
    """Whether a tool's first use in a conversation runs without asking: the instance
    setting ``tool_permissions.auto_approve``, read now through the settings accessor
    (``config.yaml`` in personal mode, the administrator's setting in server mode).
    Off when it cannot be read, so the dialogue is never skipped by mistake."""
    from spark.instance_settings import accessor

    try:
        return accessor.get(_AUTO_APPROVE_TOOLS) is True
    except Exception:  # noqa: BLE001 - an unreadable setting asks, as the default does
        return False


@dataclass(frozen=True)
class TurnCallbacks:
    """One turn's interactive callbacks: tool approval, agent model approval and
    agent cancellation tokens. A stream passes its own with its turn, so concurrent
    turns never answer each other's prompts."""

    tool_permission: Callable | None = None
    agent_model: Callable | None = None
    agent_cancel_register: Callable[[str], CancellationToken] | None = None


class ConversationManager:
    """Orchestrates conversation lifecycle, message handling, and tool integration.

    This is the central component tying together the database, LLM providers,
    tools, MCP servers, memory, and context compaction.

    One manager serves every user of a server. A turn's user guid is passed down
    each call (memory tools, memory retrieval, sub-agents, the knowledge graph),
    never kept on the instance; ``user_guid`` given here is personal mode's single
    local user, used only when a call names no user outside server mode.
    """

    def __init__(
        self,
        db: SyncDatabase,
        models: ModelSource,
        *,
        global_instructions: str | None = None,
        max_tool_iterations: int = 25,
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
        agent_cancel_register: Callable[[str], CancellationToken] | None = None,
        embedded_tools_config: dict[str, Any] | None = None,
        index_config: dict[str, Any] | None = None,
        prompt_caching: bool = True,
    ) -> None:
        self._db = db
        # The connections every model is reached through (M3): a turn, a sub-agent,
        # a compaction or a debate asks it for a service of its own.
        self._models = models
        self._limits = _CatalogueLimits(models)
        self._global_instructions = global_instructions
        self._max_tool_iterations = max_tool_iterations
        self._max_tool_result_tokens = max_tool_result_tokens
        self._mcp_manager = mcp_manager
        self._user_guid = user_guid
        self._mcp_loop = mcp_loop
        self._tool_permission_callback = tool_permission_callback
        self._agent_model_callback = agent_model_callback
        self._agent_cancel_register = agent_cancel_register
        self._embedded_tools_config = embedded_tools_config or {}
        self._index_config = index_config or {}
        self._prompt_caching_enabled = prompt_caching
        self._in_tool_use_loop = False

        # Memory indexes for tool use, one per user
        self._memory_indexes: dict[str, Any] = {}
        self._memory_indexes_lock = threading.Lock()

        # Cache embedded tool definitions
        self._builtin_tools: list[dict[str, Any]] | None = None
        # Set of builtin tool names for fast lookup
        self._builtin_tool_names: set[str] | None = None

        # Per-conversation vector index cache, keyed by (user guid, conversation)
        self._vector_indices: dict[tuple[str, int], Any] = {}

        # Context compactor (conduit's compaction layer): each turn passes the service it
        # compacts with (its own, or one for the locked compaction model). Limits come
        # from the catalogue. Its thresholds are read again before each check.
        self._compaction_settings = CompactionSettings(
            rollup_threshold, emergency_rollup_threshold, rollup_summary_ratio
        ).checked()
        self._compactor: ContextCompactor | None = ContextCompactor(
            None,
            db,
            self._limits,
            threshold=self._compaction_settings.threshold,
            emergency_threshold=self._compaction_settings.emergency_threshold,
            summary_ratio=self._compaction_settings.summary_ratio,
            locked_model=compaction_model,
        )

    def update_compaction_settings(
        self,
        *,
        rollup_threshold: float | None = None,
        emergency_rollup_threshold: float | None = None,
        rollup_summary_ratio: float | None = None,
    ) -> None:
        """Use new compaction thresholds from the next compaction check on (a settings
        change). A server also reads its instance settings before every check."""
        current = self._compaction_settings
        self._compaction_settings = CompactionSettings(
            current.threshold if rollup_threshold is None else rollup_threshold,
            (
                current.emergency_threshold
                if emergency_rollup_threshold is None
                else emergency_rollup_threshold
            ),
            current.summary_ratio if rollup_summary_ratio is None else rollup_summary_ratio,
        ).checked()

    def update_prompt_caching(self, enabled: bool) -> None:
        """Use prompt caching (or not) from the next model call on (a settings change)."""
        self._prompt_caching_enabled = bool(enabled)

    def _live_compaction_settings(self) -> CompactionSettings:
        """The compaction thresholds now: a server's instance settings, else this manager's."""
        own = self._compaction_settings

        def ratio(key: str, fallback: float) -> float:
            value = _live_setting(key, fallback)
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        return CompactionSettings(
            ratio(_ROLLUP_THRESHOLD, own.threshold),
            ratio(_EMERGENCY_ROLLUP_THRESHOLD, own.emergency_threshold),
            ratio(_ROLLUP_SUMMARY_RATIO, own.summary_ratio),
        ).checked()

    def _live_prompt_caching(self, conv: dict | None) -> bool:
        """Whether this call caches its prompt: the conversation's own choice, else a
        server's instance setting, else this manager's."""
        if conv and conv.get("prompt_caching") is not None:
            return bool(conv.get("prompt_caching", True))
        return bool(_live_setting(_PROMPT_CACHING, self._prompt_caching_enabled))

    def update_embedded_tools_config(self, config: dict[str, Any]) -> None:
        """Use a new embedded tools configuration from the next turn on (a settings change).

        Keys set at run time (``_``-prefixed, such as the memory index and prompt
        inspection) are kept unless ``config`` carries them; tool definitions are
        rebuilt on next use.
        """
        updated = dict(config)
        for key, value in self._embedded_tools_config.items():
            if (
                key.startswith("_")
                and key not in updated
                and not key.startswith("_prompt_inspection")
            ):
                updated[key] = value
        self._embedded_tools_config = updated
        self._builtin_tools = None
        self._builtin_tool_names = None

    def update_tool_limits(
        self,
        *,
        max_tool_iterations: int | None = None,
        max_tool_result_tokens: int | None = None,
    ) -> None:
        """Apply new tool execution limits from the next turn on (a settings change)."""
        if max_tool_iterations is not None:
            self._max_tool_iterations = max_tool_iterations
        if max_tool_result_tokens is not None:
            self._max_tool_result_tokens = max_tool_result_tokens

    def _get_vector_index(self, conversation_id: int, user_guid: str) -> Any:
        """Get or create the vector index for one of the user's conversations."""
        key = (user_guid, conversation_id)
        if key not in self._vector_indices:
            try:
                from spark.index.vector_index import ConversationVectorIndex

                self._vector_indices[key] = ConversationVectorIndex(
                    self._db,
                    conversation_id,
                    user_guid,
                    self._index_config,
                )
            except Exception as e:
                logger.debug("Vector index not available: %s", e)
                return None
        return self._vector_indices.get(key)

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
        self,
        conversation_id: int,
        user_guid: str,
        tool_name: str,
        tool_input: dict,
        msg_id: int,
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
        self,
        conversation_id: int,
        user_guid: str,
        query: str,
        conv: dict,
        *,
        shared: bool = False,
    ) -> str | None:
        """Retrieve relevant context from the vector index for augmentation.

        Searches the current conversation AND any linked conversations, except while the
        conversation is ``shared``: linked conversations are the owner's own, and every
        member reads the answer, so only this conversation is searched.
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

            active_msgs = self._db.call(msg_db.get_messages, user_guid, conversation_id)
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

                linked = (
                    []
                    if shared
                    else self._db.call(conversation_links.get_links, conversation_id, user_guid)
                )
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
        """Built-in tool definitions this deployment offers, whatever the user's role (cached).

        A turn offers the user only those their permissions allow (:meth:`_get_all_tools`).
        """
        if self._builtin_tools is None:
            from spark.tools.registry import ANY_ROLE, PERMISSIONS_KEY, get_builtin_tools

            self._builtin_tools = get_builtin_tools(
                {**self._embedded_tools_config, PERMISSIONS_KEY: ANY_ROLE}
            )
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

    def _get_all_tools(self, permissions: Permissions = None) -> list[dict[str, Any]]:
        """Get all tools: built-in + MCP server tools, as ``permissions`` allow."""
        from spark.tools.registry import is_tool_available

        tools = [
            t
            for t in self._get_builtin_tools()
            if is_tool_available(t["name"], permissions=permissions)
        ]

        if self._mcp_manager:
            try:
                import asyncio

                # Only the MCP servers ``permissions`` grant (server mode, A12).
                def _listing() -> Any:
                    return self._mcp_manager.list_all_tools(permissions=permissions)

                # list_all_tools is async — dispatch on the persistent MCP loop
                if self._mcp_loop and self._mcp_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(_listing(), self._mcp_loop)
                    mcp_tools = future.result(timeout=10)
                else:
                    # Fallback for cases without a persistent loop (e.g. tests)
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            import concurrent.futures

                            with concurrent.futures.ThreadPoolExecutor() as pool:
                                mcp_tools = pool.submit(lambda: asyncio.run(_listing())).result(
                                    timeout=10
                                )
                        else:
                            mcp_tools = loop.run_until_complete(_listing())
                    except RuntimeError:
                        mcp_tools = asyncio.run(_listing())

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
        tools.extend(
            t
            for t in self._get_agent_tools()
            if is_tool_available(t["name"], permissions=permissions)
        )

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

        # Each turn takes a service of its own for the conversation's model, so
        # nothing shared changes with one user's choice.
        return int(
            self._db.call(
                conversations.create_conversation,
                name,
                model_id,
                user_guid,
                instructions=instructions,
                web_search_enabled=web_search_enabled,
            )
        )

    def get_conversations(self, user_guid: str) -> list[dict]:
        """Get all active conversations."""
        from spark.database import conversations

        return self._db.call(conversations.get_active_conversations, user_guid)

    def get_conversation(self, conversation_id: int, user_guid: str) -> dict | None:
        """Get a single conversation."""
        from spark.database import conversations

        return self._db.call(conversations.get_conversation, conversation_id, user_guid)

    def delete_conversation(self, conversation_id: int, user_guid: str) -> None:
        """Soft-delete a conversation."""
        from spark.database import conversations

        self._db.call(conversations.delete_conversation, conversation_id, user_guid)

    # -- Message handling -----------------------------------------------------

    def get_messages(self, conversation_id: int, user_guid: str) -> list[dict]:
        """Messages of one of the user's conversations, for UI display (none for another's).

        Includes rolled-up messages so the user sees the full chat history
        when reloading a compacted conversation. The LLM-context loader
        (`_get_messages_for_model`) keeps the default filter and excludes
        rolled-up messages, preserving the token-saving effect of compaction.
        """
        from spark.database import messages

        return self._db.call(
            messages.get_messages, user_guid, conversation_id, include_rolled_up=True
        )

    def get_reasoning(self, conversation_id: int, user_guid: str) -> dict[int, str]:
        """The reasoning kept beside a conversation's messages, by message id, for the chat
        page only. It is never part of :meth:`get_messages`, so an export, the model's
        history and compaction cannot carry it."""
        from spark.database import message_reasoning

        return self._db.call(message_reasoning.get_reasoning, user_guid, conversation_id)

    def send_message(
        self,
        conversation_id: int,
        user_message: str,
        user_guid: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        stream_callback: Callable | None = None,
        status_callback: Callable | None = None,
        cancel_token: CancellationToken | None = None,
        permissions: Permissions = None,
        callbacks: TurnCallbacks | None = None,
        roles: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Send a user message and get the assistant response.

        If tools is None, automatically loads embedded built-in tools.
        ``permissions`` are the signed-in user's (server mode): the turn offers
        and runs only the tool categories they grant, and without them a server
        refuses every built-in tool. Personal mode ignores them. ``callbacks`` are this
        turn's interactive callbacks (a stream's); without them the manager's own apply.

        ``roles`` are the user's roles (server mode), which decide their monthly token
        budget. The budget is checked once, before anything of the turn is stored:
        at 100% the turn is refused with :class:`spark.usage.budgets.BudgetExceeded`,
        and a turn that started under it always finishes. Every model call the turn
        makes, its sub-agents' and its compaction's included, is recorded against
        ``user_guid``. The result's ``budget`` is the user's standing after the turn.

        In a shared conversation ``user_guid`` is the sender, whatever their role, and the
        turn is theirs throughout (CS28): their credits and budget, their tools and
        prompts, their usage. While the conversation has any member besides its owner,
        every turn (the owner's too, since every member reads the answer) runs without
        memory or knowledge graph tools, without retrieval from linked conversations and
        without knowledge graph context.
        """
        from spark.database import conversation_members, conversations
        from spark.database import messages as msg_db

        callbacks = callbacks or self._default_callbacks()

        conv = self._db.call(conversations.get_conversation, conversation_id, user_guid)
        if not conv:
            raise ValueError(f"Conversation {conversation_id} not found")
        shared = bool(self._db.call(conversation_members.is_shared, user_guid, conversation_id))
        if shared:
            # Memories and knowledge graphs are withheld from the whole turn, its
            # sub-agents included.
            permissions = _without_private(permissions)

        # The conversation's model, as a (connection, model) pair. On a server the
        # user may use only a connection assigned to them (M9): checked every turn.
        model_id = conv.get("model_id", "")
        ref = self._models.resolve(model_id) if model_id else None
        self._check_assigned(ref, user_guid, roles, permissions)

        # A new turn is refused once the user has used their monthly budget.
        budgets.check(
            self._db,
            user_guid,
            roles,
            connection_id=ref.connection_id if ref is not None else None,
        )

        # This turn's own service for the conversation's model, billed to its user.
        service = self._turn_service(
            model_id, Attribution(user_guid, "chat", conversation_id=conversation_id)
        )

        def count_tokens(text: str) -> int:
            return service.count_tokens(text) if service is not None else len(text) // 4

        # Auto-load embedded tools + MCP tools if none provided
        if tools is None:
            tools = self._get_all_tools(permissions)
        if shared:
            # Personal memories and knowledge graphs are left out (CS28, CS31).
            tools = [t for t in tools if t.get("name") not in PRIVATE_TOOLS]

        # Files the turn carries (attached to the message or to the conversation):
        # read_my_file and list_my_files go first, and one line names the files.
        tools, attachments_line = self._present_attachments(
            tools, conversation_id, user_guid, user_message
        )

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
                            "status": "completed",
                        }
        except Exception as e:
            logger.debug("Prompt inspection error (non-fatal): %s", e)

        # Add user message
        token_count = count_tokens(user_message)
        user_msg_id = self._db.call(
            msg_db.add_message, conversation_id, "user", user_message, token_count, user_guid
        )

        # Index user message in vector store
        self._index_message(conversation_id, user_guid, user_msg_id, "user", user_message)

        # Retrieve relevant context from vector index (respects conv settings)
        retrieved_context = self._retrieve_relevant_context(
            conversation_id, user_guid, user_message, conv, shared=shared
        )
        if retrieved_context:
            logger.info(
                "RAG context injected into system prompt (%d chars)",
                len(retrieved_context),
            )
        else:
            logger.debug("No RAG context retrieved for this message")

        # The user's enabled skills, through conduit's progressive disclosure: listed in
        # the system prompt, loaded into it by use_skill (failure never breaks a turn)
        skills = self._turn_skills(user_guid, conversation_id)

        # Knowledge graph context (failure-safe; graphs must never break a turn)
        kg_block = ""
        try:
            from spark.knowledge.query import subgraph_for_query
            from spark.knowledge.resolve import (
                available_scopes,
                conversation_kg_settings,
            )
            from spark.knowledge.tools import _get_embedder

            kg_settings = self._db.call(conversation_kg_settings, conversation_id, user_guid)
            # A knowledge graph stays with its owner: none in a shared conversation.
            if kg_settings["kg_auto_context"] and not shared:
                kg_scopes = self._db.call(available_scopes, conversation_id, user_guid)
                if kg_scopes:
                    subgraph = subgraph_for_query(
                        self._db, _get_embedder(), kg_scopes, user_message, user_guid
                    )
                    if subgraph:
                        kg_block = "## Knowledge Graph Context\n" + subgraph
        except Exception:  # noqa: BLE001
            logger.warning("Knowledge graph context unavailable", exc_info=True)

        # Build system instructions (with retrieved context if available). The skills
        # listing and any loaded skills follow, before the prompt's cache breakpoint;
        # a skill loaded during the turn joins the system prompt of the next call.
        base_system = self._build_system_instructions(
            conv,
            retrieved_context=retrieved_context,
            kg_block=kg_block,
            attachments_line=attachments_line,
            shared=shared,
            project_block=self._project_block(conversation_id, user_guid),
        )
        system = self._with_skills(base_system, skills)
        logger.debug(
            "System prompt: %d chars, %d messages in history",
            len(system),
            len(self._get_messages_for_model(conversation_id, user_guid, conv)),
        )

        # One unguessable delimiter for this turn's untrusted-content blocks,
        # and the per-source actions for flagged results.
        turn_id = new_turn_id()
        result_policy = ResultPolicy.from_config(self._embedded_tools_config)

        # Tool use loop
        self._in_tool_use_loop = True
        total_usage = {"input_tokens": 0, "output_tokens": 0}
        all_tool_calls: list[dict] = []
        final_content = ""
        iterations = 0

        def _is_cancelled() -> bool:
            return cancel_token is not None and cancel_token.is_cancelled()

        def _record_cancellation_marker() -> None:
            marker = (
                f"[TURN CANCELLED — {datetime.now(timezone.utc).isoformat()}]\n\n"
                "The user cancelled this turn."
            )
            token_count = 0
            try:
                if service is not None:
                    token_count = service.count_tokens(marker)
            except Exception:
                token_count = 0
            try:
                self._db.call(
                    msg_db.add_message,
                    conversation_id,
                    "user",
                    marker,
                    token_count,
                    user_guid,
                )
            except NotOwned:
                # The sender was removed from the shared conversation: nothing more is
                # written in their name.
                pass

        try:
            for iteration in range(self._max_tool_iterations):
                iterations = iteration + 1

                # Cooperative cancellation check at the top of the loop —
                # before invoking the LLM for this iteration.
                if _is_cancelled():
                    _record_cancellation_marker()
                    return {
                        "content": final_content,
                        "usage": total_usage,
                        "tool_calls": all_tool_calls,
                        "iterations": iterations,
                        "status": "cancelled",
                    }

                # Get messages for model (respects conv settings)
                history = self._get_messages_for_model(conversation_id, user_guid, conv)
                if iteration:
                    system = self._with_skills(base_system, skills)

                # Invoke model (with error recovery)
                try:
                    # Prompt caching: the conversation's own choice, else the setting now
                    use_caching = self._live_prompt_caching(conv)

                    if service is None:
                        raise RuntimeError(
                            f"the model '{model_id}' is not available on any connection"
                        )
                    live_reasoning: list[str] = []

                    def _reasoning_arrived(text: str, _seen: list[str] = live_reasoning) -> None:
                        """Reasoning the service reports as it arrives (before the answer)."""
                        if text and text.strip():
                            _seen.append(text)
                            if status_callback:
                                status_callback("reasoning", {"text": text})

                    response = service.invoke_model(
                        history,
                        max_tokens=self._limits.get_max_output(model_id),
                        temperature=0.7,
                        tools=tools if tools else None,
                        system=system,
                        stream_callback=stream_callback,
                        reasoning_callback=_reasoning_arrived,
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
                    self._db.call(
                        msg_db.add_message,
                        conversation_id,
                        "assistant",
                        error_content,
                        count_tokens(error_content),
                        user_guid,
                    )
                    return {
                        "content": error_content,
                        "usage": total_usage,
                        "tool_calls": all_tool_calls,
                        "iterations": iterations,
                        "error": True,
                        "status": "completed",
                    }

                # Cooperative cancellation check immediately after the LLM
                # returns — cancellation may have been requested during the
                # invocation (e.g. while streaming).
                if _is_cancelled():
                    _record_cancellation_marker()
                    return {
                        "content": final_content,
                        "usage": total_usage,
                        "tool_calls": all_tool_calls,
                        "iterations": iterations,
                        "status": "cancelled",
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

                # The model's reasoning is shown beside the reply, never in it. A service
                # that reported it live has already sent it; otherwise it is sent now.
                reasoning = response_reasoning(response.get("content_blocks"))
                if reasoning and not live_reasoning and status_callback:
                    status_callback("reasoning", {"text": reasoning})

                if stop_reason == "tool_use" and response.get("tool_use"):
                    # Execute tools and continue loop
                    tool_results = self._execute_tools(
                        conversation_id,
                        response["tool_use"],
                        user_guid,
                        status_callback=status_callback,
                        turn_id=turn_id,
                        result_policy=result_policy,
                        permissions=permissions,
                        callbacks=callbacks,
                        skills=skills,
                    )
                    all_tool_calls.extend(response["tool_use"])

                    # Store assistant message with tool calls
                    assistant_content = response.get("content_blocks", [])
                    assistant_text = json.dumps(assistant_content) if assistant_content else ""
                    assistant_tokens = count_tokens(assistant_text)
                    asst_msg_id = self._db.call(
                        msg_db.add_message,
                        conversation_id,
                        "assistant",
                        assistant_text,
                        assistant_tokens,
                        user_guid,
                    )
                    self._keep_reasoning(conversation_id, user_guid, asst_msg_id, reasoning)

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
                    results_tokens = count_tokens(results_text)
                    self._db.call(
                        msg_db.add_message,
                        conversation_id,
                        "user",
                        f"{_TOOL_RESULTS_MARKER}{results_text}",
                        results_tokens,
                        user_guid,
                    )

                    # Update token usage
                    self._db.call(
                        conversations.update_token_usage,
                        conversation_id,
                        model_id,
                        resp_usage.get("input_tokens", 0),
                        resp_usage.get("output_tokens", 0),
                        user_guid,
                    )

                    if status_callback:
                        status_callback("tool_iteration_complete", {"iteration": iterations})

                    # Cooperative cancellation check after tools have executed —
                    # avoids re-invoking the LLM for another iteration.
                    if _is_cancelled():
                        _record_cancellation_marker()
                        return {
                            "content": final_content,
                            "usage": total_usage,
                            "tool_calls": all_tool_calls,
                            "iterations": iterations,
                            "status": "cancelled",
                        }

                    continue

                # Final text response
                final_content = response.get("content", "")
                final_tokens = count_tokens(final_content)
                final_msg_id = self._db.call(
                    msg_db.add_message,
                    conversation_id,
                    "assistant",
                    final_content,
                    final_tokens,
                    user_guid,
                )
                self._keep_reasoning(conversation_id, user_guid, final_msg_id, reasoning)

                # Index assistant response in vector store
                self._index_message(
                    conversation_id, user_guid, final_msg_id, "assistant", final_content
                )

                self._db.call(
                    conversations.update_token_usage,
                    conversation_id,
                    model_id,
                    resp_usage.get("input_tokens", 0),
                    resp_usage.get("output_tokens", 0),
                    user_guid,
                )
                break

        except NotOwned:
            # The sender lost access during the turn (removed from a shared conversation):
            # the turn ends as cancelled and writes nothing more in their name.
            return {
                "content": final_content,
                "usage": total_usage,
                "tool_calls": all_tool_calls,
                "iterations": iterations,
                "status": "cancelled",
            }
        finally:
            self._in_tool_use_loop = False
            if skills is not None:
                skills.close()

        # Check for deferred compaction
        self._check_compaction(conversation_id, model_id, user_guid, status_callback, service)

        return {
            "content": final_content,
            "usage": total_usage,
            "tool_calls": all_tool_calls,
            "iterations": iterations,
            "status": "completed",
            "budget": self._budget_after(user_guid, roles),
        }

    def _keep_reasoning(
        self, conversation_id: int, user_guid: str, message_id: int, reasoning: str
    ) -> None:
        """Keep a reply's reasoning beside its message (failure never breaks a turn)."""
        if not reasoning:
            return
        from spark.database import message_reasoning

        try:
            self._db.call(
                message_reasoning.add_reasoning, user_guid, conversation_id, message_id, reasoning
            )
        except Exception:  # noqa: BLE001 - the reasoning is only for display
            logger.warning("Could not keep a message's reasoning", exc_info=True)

    # -- Skills -----------------------------------------------------------------

    def _present_attachments(
        self,
        tools: list[dict[str, Any]],
        conversation_id: int,
        user_guid: str,
        user_message: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """The turn's tools, with read_my_file and list_my_files first when the turn
        carries files, and the line naming those files by id (empty when none).

        The files are the turn's user's own: those the message's note names and
        those uploaded to this conversation. No tool is added or removed. Failure
        never breaks a turn.
        """
        if not any(t.get("name") == "read_my_file" for t in tools):
            return tools, ""
        uid = self._turn_user(user_guid)
        if uid is None:
            return tools, ""
        try:
            from spark.tools import my_files

            attached = my_files.attached_files(self._db, uid, conversation_id, user_message)
        except Exception:  # noqa: BLE001 - attachments must never break a turn
            logger.warning("Attached files unavailable for this turn", exc_info=True)
            return tools, ""
        if not attached:
            return tools, ""
        return my_files.attachment_tools_first(tools), my_files.attachment_context(attached)

    def _turn_skills(self, user_guid: str, conversation_id: int) -> Any:
        """This turn's skills (:class:`spark.skills.session.TurnSkills`): the skills the
        user has enabled, globally and for the conversation, with the skills the
        conversation loaded in earlier turns restored in order. None when skills are
        unavailable (a turn never fails over skills)."""
        from spark.connections.transcript_store import skill_transcript
        from spark.database import messages
        from spark.database import skills as skills_db
        from spark.skills.manager import get_skills_manager
        from spark.skills.session import TurnSkills

        skills = None
        try:
            manager = get_skills_manager()
            enabled = self._db.call(skills_db.resolve_enabled, manager, user_guid, conversation_id)
            skills = TurnSkills(
                manager.source(enabled=[s["name"] for s in enabled]),
                folders=manager.folders(),
            )
            stored = self._db.call(
                messages.get_messages, user_guid, conversation_id, include_rolled_up=True
            )
            skills.restore(skill_transcript(str(conversation_id), stored))
            return skills
        except Exception:  # noqa: BLE001 - skills must never break a chat turn
            logger.warning("Skills unavailable this turn", exc_info=True)
            if skills is not None:
                skills.close()
            return None

    @staticmethod
    def _with_skills(base: str, skills: Any) -> str:
        """``base`` with the skills listing and loaded skills after it."""
        if skills is None:
            return base
        try:
            return str(skills.system(base))
        except Exception:  # noqa: BLE001 - skills must never break a chat turn
            logger.warning("Skills listing unavailable", exc_info=True)
            return base

    def _budget_after(self, user_guid: str, roles: Iterable[str] | None) -> dict[str, Any] | None:
        """The user's budget standing after a turn, for the UI's warning (None if unreadable)."""
        try:
            status = self._db.call(budgets.status_for, user_guid, roles)
        except Exception:  # noqa: BLE001 - reporting the standing must never fail a turn
            logger.warning("Could not read the budget after a turn", exc_info=True)
            return None
        return status.as_dict()

    # -- System instructions --------------------------------------------------

    def _build_conversation_tool_context(self, model_id: str = "") -> str:
        """Build additional system prompt context for enabled tools (``model_id``: the turn's)."""
        from spark.tools.registry import is_category_available

        embedded = self._embedded_tools_config.get("embedded_tools", {})
        parts: list[str] = []

        # Filesystem allowed paths
        fs_config = embedded.get("filesystem", {})
        allowed_paths = fs_config.get("allowed_paths", [])
        if isinstance(allowed_paths, str):
            allowed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]
        if is_category_available("filesystem") and fs_config.get("enabled", True) and allowed_paths:
            path_list = ", ".join(f"`{p}`" for p in allowed_paths)
            parts.append(
                "**Filesystem access:** You can read"
                + (" and write" if fs_config.get("mode") == "read_write" else "")
                + f" files within these paths: {path_list}\n"
            )

        # System commands
        cmd_config = embedded.get("system_commands", {})
        if is_category_available("system_commands") and cmd_config.get("enabled", False):
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
        if (
            is_category_available("email")
            and email_config.get("enabled", False)
            and email_config.get("host")
        ):
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
                    models = self._connection_models(model_id)
                    if models:
                        model_lines = []
                        for m in models[:10]:
                            ctx = m.get("context_length", "?")
                            if isinstance(ctx, int) and ctx >= 1000:
                                ctx = f"{ctx // 1000}K"
                            model_lines.append(f"  - `{m['id']}` ({m.get('name', '')}, {ctx} ctx)")
                        agent_desc += "Available models:\n" + "\n".join(model_lines) + "\n"
                except Exception:
                    pass
            else:
                agent_desc += "Agents will use the same model as this conversation.\n"

            parts.append(agent_desc)

        if parts:
            return "\n".join(parts) + "\n"
        return ""

    def _project_block(self, conversation_id: int, user_guid: str) -> str:
        """The project context of a conversation in a project, read afresh every turn (a
        moved conversation takes its new level's from its next turn), or "". A failure is
        logged and the turn goes on without it."""
        from spark.projects import prompt as project_prompt

        try:
            context = self._db.call(project_prompt.conversation_context, user_guid, conversation_id)
        except Exception:  # noqa: BLE001 - a project's context never breaks a turn
            logger.warning("Project context unavailable", exc_info=True)
            return ""
        return context.render() if context is not None else ""

    def _build_system_instructions(
        self,
        conv: dict,
        *,
        retrieved_context: str | None = None,
        skills_block: str = "",
        kg_block: str = "",
        attachments_line: str = "",
        shared: bool = False,
        project_block: str = "",
    ) -> str:
        """Assemble system instructions from all sources.

        A ``shared`` conversation is read by several people: the instructions say so, and
        leave the memory tools out (none are offered). ``project_block`` is the project
        context of a conversation in a project (:meth:`_project_block`): it follows the
        global instructions and comes before the conversation's own (PC7, PC8).
        """
        parts: list[str] = []

        memory = (
            (
                "**Shared conversation:** This conversation is shared between several people; "
                "each message names its sender. Personal memories are not available here.\n\n"
            )
            if shared
            else (
                f"**Memory tools:** You can store and retrieve persistent information across conversations.\n"
                f"- `store_memory` — Save facts, preferences, projects, instructions, or relationships the user shares.\n"
                f"- `query_memory` — Search stored memories by semantic relevance. Use this to recall information from past conversations.\n"
                f"- `list_memories` — List all stored memories, optionally filtered by category.\n"
                f"- `delete_memory` — Remove a specific memory by ID.\n\n"
                f"**When the user tells you something about themselves, their work, or their preferences, proactively use `store_memory` to remember it.**\n"
                f"**When the user asks about something that might have been discussed before, use `query_memory` to check.**\n\n"
            )
        )

        # 1. Embedded identity and capabilities
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts.append(
            f"You are Spark, a secure personal AI research assistant.\n"
            f"Current date/time: {now}\n\n"
            f"## Your Capabilities\n\n"
            f"You have access to tools that you should actively use when appropriate:\n\n"
            + memory
            + f"**Other tools:** You may also have access to filesystem, document, web search, email, and other tools. "
            f"Use them when the user's request requires reading files, searching the web, or performing other actions.\n\n"
            + self._build_conversation_tool_context(conv.get("model_id") or "")
            + "**Tool documentation:** Use the `get_tool_documentation` tool to retrieve detailed usage instructions, "
            "parameter references, examples, and best practices for any tool before using it. "
            "Pass `_index` as the tool name to see all available documentation.\n\n"
            "**Linked conversations:** If this conversation is linked to others, relevant context from those conversations "
            "is automatically provided below. You can reference this context in your responses."
        )

        # External tool results arrive as marked, untrusted data
        parts.append(UNTRUSTED_CONTENT_INSTRUCTIONS)

        # 2. Global instructions
        if self._global_instructions:
            parts.append(self._global_instructions)

        # 2a. The project path's instructions and resources, top level first
        if project_block:
            parts.append(project_block)

        # 3. Conversation-specific instructions (after the project's: they come last)
        conv_instructions = conv.get("instructions")
        if conv_instructions:
            parts.append(conv_instructions)

        # The files this turn carries, by id, and the call that reads them (never content)
        if attachments_line:
            parts.append(attachments_line)

        # 4. Retrieved context from vector index and linked conversations
        if retrieved_context:
            parts.append(retrieved_context)

        if skills_block:
            parts.append(skills_block)

        if kg_block:
            parts.append(kg_block)

        return "\n\n".join(parts)

    # -- Message formatting ---------------------------------------------------

    def _get_messages_for_model(
        self, conversation_id: int, user_guid: str, conv: dict | None = None
    ) -> list[dict[str, Any]]:
        """Format the stored messages of one of the user's conversations for the LLM API.

        Respects per-conversation settings:
        - max_history_messages: limit to last N messages (None = all)
        - include_tool_results: whether to include tool result messages
        """
        from spark.database import messages

        # A compaction's summary is stored after the turns it keeps; it is read first.
        raw = ordered_for_model(self._db.call(messages.get_messages, user_guid, conversation_id))
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
        turn_id: str | None = None,
        result_policy: ResultPolicy | None = None,
        permissions: Permissions = None,
        callbacks: TurnCallbacks | None = None,
        skills: Any = None,
    ) -> dict:
        """Execute a single tool call and return the tool_result dict.

        Extracted from _execute_tools so that agent calls can be dispatched
        to a thread pool for parallel execution.
        """
        from spark.database import mcp_ops, tool_permissions

        callbacks = callbacks or self._default_callbacks()
        ask = callbacks.tool_permission
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
        permission = self._db.call(
            tool_permissions.is_tool_allowed, user_guid, conversation_id, tool_name
        )

        # A forced prompt asks every time, whatever was allowed before and whatever
        # the auto-approve setting says.
        forced = False

        # System commands: always prompt when require_approval is on,
        # unless running without a callback (autonomous actions).
        if (
            tool_name == "run_command"
            and ask
            and self._embedded_tools_config.get("embedded_tools", {})
            .get("system_commands", {})
            .get("require_approval", True)
        ):
            permission = None  # Force re-prompt
            forced = True

        # Email send_email: always prompt when require_approval is on,
        # unless running without a callback (autonomous actions).
        if (
            tool_name == "send_email"
            and ask
            and self._embedded_tools_config.get("embedded_tools", {})
            .get("email", {})
            .get("require_approval", True)
        ):
            permission = None  # Force re-prompt
            forced = True

        # Skill authoring: always prompt, even when previously always-allowed,
        # unless running without a callback (autonomous actions).
        if tool_name in ("create_skill", "update_skill") and ask:
            permission = None  # Force re-prompt
            forced = True

        # Skill loading is transparent: use_skill / read_skill_resource never
        # prompt for approval. Loading a skill is a read-only progressive-disclosure
        # step, so it should be invisible to the user; authoring (above) stays gated.
        from spark.skills.tools import READ_TOOL_NAMES as _SKILL_READ_TOOLS

        if tool_name in _SKILL_READ_TOOLS:
            permission = True

        # The user's own file area is theirs to read: listing and reading it never
        # prompt (wave 5 Q27). The tools themselves are scoped to the user.
        if tool_name in ("list_my_files", "read_my_file"):
            permission = True

        # Auto-approve Tools: a first use (nothing stored for this tool) runs without
        # the dialogue, for this call only; a deny stays and forced prompts still ask.
        if permission is None and ask and not forced and _auto_approve_tools():
            permission = True

        if permission is None:
            # First use — prompt user
            if ask:
                decision = ask(tool_name, tool_input)
                if decision in ("allowed", "allowed_global", "once"):
                    if decision == "allowed":
                        # Approve this tool and category siblings for this conversation
                        for sibling in _get_tool_category_siblings(tool_name):
                            self._db.call(
                                tool_permissions.set_tool_permission,
                                conversation_id,
                                sibling,
                                "allowed",
                                user_guid,
                            )
                    elif decision == "allowed_global":
                        # Approve this tool and category siblings globally
                        for sibling in _get_tool_category_siblings(tool_name):
                            self._db.call(
                                tool_permissions.set_global_tool_permission,
                                user_guid,
                                sibling,
                                "allowed",
                            )
                else:
                    self._db.call(
                        tool_permissions.set_tool_permission,
                        conversation_id,
                        tool_name,
                        "denied",
                        user_guid,
                    )
                    return _tool_result(
                        tool_id,
                        "The user denied permission for this tool call. Do not retry this exact call. "
                        "Consider whether the task can be completed without it, or ask the user how "
                        "they would like to proceed.",
                        is_error=True,
                    )
            # No callback — auto-allow (for testing or auto-approve mode)
        elif permission is False:
            return _tool_result(
                tool_id,
                "The user denied permission for this tool call. Do not retry this exact call. "
                "Consider whether the task can be completed without it, or ask the user how "
                "they would like to proceed.",
                is_error=True,
            )

        # Execute
        logger.info(
            "Tool call: %s (id=%s) params=%s",
            tool_name,
            tool_id,
            json.dumps(tool_input)[:200],
        )
        start_time = time.monotonic()
        try:
            result_text, is_error = self._call_tool(
                tool_name,
                tool_input,
                permissions=permissions,
                conversation_id=conversation_id,
                user_guid=user_guid,
                status_callback=status_callback,
                callbacks=callbacks,
                skills=skills,
            )
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
        self._db.call(
            mcp_ops.record_transaction,
            conversation_id,
            tool_name,
            json.dumps(tool_input),
            result_text,
            user_guid,
            is_error=is_error,
            execution_time_ms=elapsed_ms,
        )

        # External results reach the model only as marked, inspected data
        from spark.tools.registry import tool_category

        model_text = result_text
        inspection: dict[str, Any] | None = None
        outcome = guard_tool_result(
            result_text,
            tool_name=tool_name,
            mcp=tool_category(tool_name) is None and not self._is_builtin_tool(tool_name),
            turn_id=turn_id or new_turn_id(),
            policy=result_policy or ResultPolicy.from_config(self._embedded_tools_config),
            db=self._db,
            user_guid=user_guid,
            conversation_id=conversation_id,
        )
        if outcome is not None:
            model_text = outcome.text
            if outcome.flagged:
                inspection = outcome.event()

        if status_callback:
            event: dict[str, Any] = {
                "tool_use_id": tool_id,
                "tool_name": tool_name,
                "result": result_text[:500],
                "status": "error" if is_error else "success",
            }
            if inspection is not None:
                event["inspection"] = inspection
            status_callback("tool_result", event)

        return _tool_result(tool_id, model_text, is_error=is_error)

    def _execute_tools(
        self,
        conversation_id: int,
        tool_calls: list[dict],
        user_guid: str,
        *,
        status_callback: Callable | None = None,
        turn_id: str | None = None,
        result_policy: ResultPolicy | None = None,
        permissions: Permissions = None,
        callbacks: TurnCallbacks | None = None,
        skills: Any = None,
    ) -> list[dict]:
        """Execute a batch of tool calls and return results.

        Non-agent tools are processed sequentially (preserving existing
        behaviour).  Multiple ``spawn_agent`` calls are dispatched in
        parallel via a thread pool so that independent agents can run
        concurrently.
        """
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

        guard: dict[str, Any] = {
            "turn_id": turn_id or new_turn_id(),
            "result_policy": result_policy,
            "permissions": permissions,
            "callbacks": callbacks,
            "skills": skills,
        }

        # 1. Execute non-agent tools sequentially.
        for idx, tc in non_agent_calls:
            result_map[idx] = self._execute_single_tool(
                tc, conversation_id, user_guid, status_callback=status_callback, **guard
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
                        **guard,
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
                tc, conversation_id, user_guid, status_callback=status_callback, **guard
            )

        # 3. Reassemble results in the original tool_calls order.
        return [result_map[i] for i in range(len(tool_calls))]

    def _get_memory_index_for_tools(self, user_guid: str) -> Any:
        """The user's MemoryIndex for tool use (created once per user), or None."""
        with self._memory_indexes_lock:
            if user_guid not in self._memory_indexes:
                try:
                    from spark.index.memory_index import MemoryIndex

                    self._memory_indexes[user_guid] = MemoryIndex(self._db, user_guid)
                except Exception:
                    return None
            return self._memory_indexes[user_guid]

    def _default_callbacks(self) -> TurnCallbacks:
        """The callbacks given to the manager (an action or a test without a stream)."""
        return TurnCallbacks(
            tool_permission=self._tool_permission_callback,
            agent_model=self._agent_model_callback,
            agent_cancel_register=self._agent_cancel_register,
        )

    def _turn_user(self, user_guid: str | None) -> str | None:
        """The user a tool call acts for.

        A turn always names its user. Without one, personal mode acts for its single
        local user; a server acts for no one (never the process's own guid).
        """
        if user_guid:
            return user_guid
        from spark import mode

        try:
            server = mode.is_server()
        except RuntimeError:
            server = False
        return None if server else self._user_guid

    def _turn_service(self, model_id: str, attribution: Attribution | None = None) -> Any:
        """A service of the turn's own for its conversation's ``model_id``, billed as
        ``attribution`` says; None when no running connection offers the model."""
        service = self._models.service(model_id, attribution=attribution) if model_id else None
        if service is None:
            logger.warning("Model %r is not available on any connection", model_id)
        return service

    def _check_assigned(
        self,
        ref: ModelRef | None,
        user_guid: str,
        roles: Iterable[str] | None,
        permissions: Permissions,
    ) -> None:
        """On a server, refuse a turn on a connection not assigned to the user (M9).

        Personal mode's single user may use every connection. A model no connection
        offers is left to the turn, which reports it.
        """
        if ref is None or not self._server():
            return
        if not self._models.may_use(
            ref.connection_id, self._user_scope(user_guid, roles, permissions)
        ):
            raise ConnectionNotAssigned(
                f"The connection '{self._models.connection_name(ref.connection_id)}' is not "
                "available to you. Choose a model from the list."
            )

    def model_refusal(
        self,
        model_id: str,
        user_guid: str,
        roles: Iterable[str] | None,
        permissions: Permissions,
    ) -> str | None:
        """Why ``user_guid`` may not send with ``model_id`` (a connection not assigned to
        them, M9), or None when they may. The page asks before a send (CS28)."""
        ref = self._models.resolve(model_id) if model_id else None
        try:
            self._check_assigned(ref, user_guid, roles, permissions)
        except ConnectionNotAssigned as refused:
            return f"You cannot use this conversation's model. {refused}"
        return None

    def _is_shared(self, conversation_id: int | None, user_guid: str | None) -> bool:
        """Whether a conversation the user may read has anyone besides its owner."""
        if conversation_id is None or not user_guid:
            return False
        from spark.database import conversation_members

        try:
            return bool(self._db.call(conversation_members.is_shared, user_guid, conversation_id))
        except Exception:  # noqa: BLE001 - when in doubt, treat it as shared
            return True

    @staticmethod
    def _user_scope(
        user_guid: str, roles: Iterable[str] | None, permissions: Permissions
    ) -> UserScope:
        """A server user, as the connections' assignment check sees them."""
        from spark.identity.permissions import ADMIN_CONNECTIONS
        from spark.tools.registry import ANY_ROLE

        administrator = (
            permissions is not None
            and permissions is not ANY_ROLE
            and ADMIN_CONNECTIONS in permissions
        )
        return UserScope(user_guid, frozenset(roles or ()), administrator=administrator)

    def _connection_models(self, model_id: str) -> list[dict[str, Any]]:
        """The models offered on the connection of a conversation's model (none when the
        model is on no running connection)."""
        ref = self._models.resolve(model_id) if model_id else None
        return self._models.listed_models(ref.connection_id) if ref is not None else []

    def _conversation_model(self, conversation_id: int | None, user_guid: str | None) -> str:
        """The model of one of the user's conversations ("" when it cannot be read)."""
        if conversation_id is None or not user_guid:
            return ""
        try:
            from spark.database import conversations

            conv = self._db.call(conversations.get_conversation, conversation_id, user_guid)
        except Exception:  # noqa: BLE001 - listing models must never break a turn
            return ""
        return str((conv or {}).get("model_id") or "")

    def _get_llm_service_for_model(self, model_id: str) -> Any:
        """A service of its own for ``model_id`` (``connection/model``, or a bare model id
        as stored before connections), or None when no running connection offers it.

        Debates, panels and knowledge graph builds take this as their service factory;
        each bills its calls itself (:func:`spark.usage.meter.meter`).
        """
        return self._models.service(model_id) if model_id else None

    def _list_provider_models(self, model_id: str = "") -> tuple[str, bool]:
        """List the models of the connection of the conversation's model (``model_id``)."""
        try:
            ref = self._models.resolve(model_id) if model_id else None
            if ref is None:
                return "No connection serves this conversation's model.", True
            name = self._models.connection_name(ref.connection_id)
            models = self._models.listed_models(ref.connection_id)
            if not models:
                return f"No models available from {name}.", False
            lines = [f"Available models from {name}:\n"]
            for m in models:
                ctx = m.get("context_length", "?")
                tools_support = "Yes" if m.get("supports_tools") else "No"
                lines.append(
                    f"- {m['id']} ({m.get('name', '')}), context: {ctx}, tools: {tools_support}"
                )
            return "\n".join(lines), False
        except Exception as e:
            return f"Error listing models: {e}", True

    @staticmethod
    def _server() -> bool:
        from spark import mode

        try:
            return mode.is_server()
        except RuntimeError:
            return False

    def _agent_model(self, requested: str, conversation_model: str) -> ModelRef | None:
        """The model a sub-agent may use: one offered on the conversation's own connection.

        A sub-agent never reaches another connection, so it never escapes the
        assignment check its conversation passed. A bare model id is looked for on that
        connection; None when the connection does not offer the model.
        """
        own = self._models.resolve(conversation_model) if conversation_model else None
        if own is None:
            return None
        parsed = ModelRef.parse(requested)
        ref = parsed if parsed is not None else ModelRef(own.connection_id, requested)
        if ref.connection_id != own.connection_id:
            return None
        offered = {m.get("model_id") for m in self._models.listed_models(own.connection_id)}
        return ref if ref.model_id in offered else None

    @staticmethod
    def _may_spawn_agents(permissions: Permissions) -> bool:
        """Whether the turn's user may spawn sub-agents: ``spark.agents`` on a server.

        Every sub-agent starts here, from a chat turn, an action or another agent,
        so this is the one gate. ``spark.tools.agents`` only offers the tool.
        Personal mode's local operator may always spawn.
        """
        from spark import mode

        try:
            server = mode.is_server()
        except RuntimeError:
            server = False
        if not server:
            return True
        from spark.identity.permissions import AGENTS
        from spark.tools.registry import ANY_ROLE

        return permissions is not None and permissions is not ANY_ROLE and AGENTS in permissions

    def _execute_agent_spawn(
        self,
        tool_input: dict,
        conversation_id: int,
        user_guid: str,
        permissions: Permissions = None,
        status_callback: Callable | None = None,
        callbacks: TurnCallbacks | None = None,
    ) -> tuple[str, bool]:
        """Spawn a sub-agent to execute a task for ``user_guid``."""
        import uuid

        from spark.core.agent_executor import AgentExecutor
        from spark.database import agents as agent_db

        task = tool_input.get("task", "").strip()
        agent_name = tool_input.get("agent_name", "Agent").strip()
        model_id = tool_input.get("model_id", "")

        if not task:
            return "Error: task description is required.", True
        conversation_model = self._conversation_model(conversation_id, user_guid)
        if model_id:
            # A sub-agent stays on its conversation's connection. The conversation's own
            # (pinned) model, used when none is named, keeps working.
            chosen = self._agent_model(model_id, conversation_model)
            if chosen is None:
                return (
                    f"Error: the model '{model_id}' is not offered on this conversation's "
                    "connection. Use list_provider_models to see the models offered, or "
                    "omit model_id.",
                    True,
                )
            model_id = str(chosen)
        if not self._may_spawn_agents(permissions):
            return (
                "Error: spawning sub-agents needs the spark.agents permission, "
                "which your role does not grant.",
                True,
            )

        # Determine model — fall back to the current conversation's model
        if not model_id:
            try:
                from spark.database import conversations

                conv = self._db.call(conversations.get_conversation, conversation_id, user_guid)
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

            conv_row = self._db.call(conv_db.get_conversation, conversation_id, user_guid)
            per_conv = (conv_row or {}).get("agent_model_selection")
            if per_conv:
                model_selection = per_conv
        except Exception:
            pass

        # When auto_select is enabled and the LLM chose a model, request user
        # approval before execution.  The callback blocks this thread until the
        # user responds (mirroring the permission_request pattern).
        callbacks = callbacks or self._default_callbacks()
        if model_selection == "auto_select" and model_id and callbacks.agent_model:
            available_models = self._connection_models(conversation_model)

            model_justification = tool_input.get("model_justification", "")
            approved_model = callbacks.agent_model(
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
                # The user's choice too must be on the conversation's connection.
                approved = self._agent_model(str(approved_model), conversation_model)
                if approved is None:
                    return (
                        f"Error: the model '{approved_model}' is not offered on this "
                        "conversation's connection.",
                        True,
                    )
                model_id = str(approved)

        agent_id = str(uuid.uuid4())[:12]
        agent_token: CancellationToken | None = None
        if callbacks.agent_cancel_register is not None:
            try:
                agent_token = callbacks.agent_cancel_register(agent_id)
            except Exception:
                agent_token = None

        # Record in database
        self._db.call(
            agent_db.create_agent_run,
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
            parent_messages = self._get_messages_for_model(conversation_id, user_guid)

        # Create and run agent — get a dedicated LLM service for the model
        # to avoid conflicts with concurrent conversations using different providers.
        try:
            agent_llm = self._get_llm_service_for_model(model_id)
            if not agent_llm:
                self._db.call(
                    agent_db.complete_agent_run,
                    user_guid,
                    agent_id,
                    status="failed",
                    result_text=f"Could not find provider for model '{model_id}'",
                )
                return f"Agent failed: no provider found for model '{model_id}'.", True

            from spark.tools.registry import PERMISSIONS_KEY

            # The sub-agent's model calls are billed to the turn's user.
            agent_llm = meter(
                agent_llm,
                self._db,
                user_guid=user_guid,
                source="agent",
                model_id=model_id,
                conversation_id=conversation_id,
            )
            executor = AgentExecutor(
                agent_llm,
                self._db,
                # The sub-agent acts for the same user, with the same permissions.
                {**self._embedded_tools_config, PERMISSIONS_KEY: permissions},
                mcp_manager=self._mcp_manager,
                mcp_loop=getattr(self, "_mcp_loop", None),
                user_guid=user_guid,
                conversation_id=conversation_id,
                tool_permission_callback=callbacks.tool_permission,
                status_callback=status_callback,
            )

            result = executor.execute(
                agent_id,
                agent_name,
                task,
                model_id,
                mode=mode,
                parent_messages=parent_messages,
                max_iterations=max_iterations,
                cancel_token=agent_token,
            )

            if result.get("status") == "cancelled":
                try:
                    self._db.call(
                        agent_db.complete_agent_run,
                        user_guid,
                        agent_id,
                        status="cancelled",
                        result_text=result.get("content", "[CANCELLED]"),
                        input_tokens=result.get("input_tokens", 0),
                        output_tokens=result.get("output_tokens", 0),
                        tool_calls_json=json.dumps(result.get("tool_calls", [])),
                    )
                except Exception as db_err:
                    logger.warning(
                        "Failed to persist cancelled status for agent %s: %s",
                        agent_id,
                        db_err,
                    )
                return (
                    "The user cancelled this agent. Do not retry the same task. "
                    "If the user wants this work done, they will provide further instructions.",
                    True,
                )

            # Record completion
            self._db.call(
                agent_db.complete_agent_run,
                user_guid,
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
            self._db.call(
                agent_db.complete_agent_run,
                user_guid,
                agent_id,
                status="failed",
                result_text=str(e),
            )
            return f"Agent '{agent_name}' failed: {e}", True

    def _call_tool(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        permissions: Permissions = None,
        conversation_id: int | None = None,
        user_guid: str | None = None,
        status_callback: Callable | None = None,
        callbacks: TurnCallbacks | None = None,
        skills: Any = None,
    ) -> tuple[str, bool]:
        """Execute a single tool for ``user_guid`` holding ``permissions``, in a turn of
        ``conversation_id``. Returns (result_text, is_error). ``skills`` is the turn's
        skill session, which ``use_skill`` loads into."""
        from spark.tools.registry import PERMISSIONS_KEY, unavailable_reason

        # A tool whose category is not available (to this mode, or to the
        # user's roles) is refused by name, wherever the request came from,
        # before any built-in or MCP dispatch.
        refusal = unavailable_reason(tool_name, permissions=permissions, user_guid=user_guid)
        if refusal is not None:
            return refusal, True

        uid = self._turn_user(user_guid)

        # Agent tools act in this turn's conversation, for this turn's user
        if tool_name == "spawn_agent":
            if conversation_id is None or uid is None:
                return "Error: no active conversation context for agent spawn.", True
            return self._execute_agent_spawn(
                tool_input, conversation_id, uid, permissions, status_callback, callbacks
            )
        if tool_name == "list_provider_models":
            return self._list_provider_models(self._conversation_model(conversation_id, uid))

        # Personal memories are left out of a shared conversation (CS28), however the
        # call was asked for.
        if tool_name in PRIVATE_TOOLS and self._is_shared(conversation_id, uid):
            return (
                "Personal memories and knowledge graphs are not available in a shared "
                "conversation.",
                True,
            )

        # Try built-in tools first
        if self._is_builtin_tool(tool_name):
            from spark.tools.registry import execute_builtin_tool

            # Memory, file and knowledge graph tools act for this turn's user only
            config = dict(self._embedded_tools_config)
            config["_memory_index"] = self._get_memory_index_for_tools(uid) if uid else None
            config["_user_guid"] = uid
            config["_kg_db"] = self._db
            config["_kg_conversation_id"] = conversation_id
            config["_kg_user_guid"] = uid
            config[PERMISSIONS_KEY] = permissions
            if skills is not None:
                from spark.skills.tools import SESSION_KEY

                config[SESSION_KEY] = skills
            return execute_builtin_tool(tool_name, tool_input, config)

        # Try MCP manager (async — dispatch on the persistent MCP event loop)
        if self._mcp_manager:
            try:
                import asyncio

                async def _call() -> dict:
                    return await self._mcp_manager.call_tool(
                        tool_name,
                        tool_input,
                        permissions=permissions,
                        user_guid=uid,
                        conversation_id=conversation_id,
                    )

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
        user_guid: str,
        status_callback: Callable | None = None,
        service: Any = None,
    ) -> None:
        """Check and perform context compaction if needed, with the turn's ``service``
        or, when a compaction model is locked, a service of its own for that model.

        Either way the compaction's model calls are recorded for ``user_guid``. The
        thresholds are the ones in force now (a server's instance settings)."""
        if self._compactor:
            self._compactor.update(self._live_compaction_settings())
            locked = self._compactor.locked_model
            locked_service = self._models.service(locked) if locked else None
            if locked_service is not None:
                service = meter(
                    locked_service,
                    self._db,
                    user_guid=user_guid,
                    source="compaction",
                    model_id=locked or "",
                    conversation_id=conversation_id,
                )
            elif service is not None and hasattr(service, "with_source"):
                service = service.with_source("compaction")
            self._compactor.check_and_compact(
                conversation_id,
                model_id,
                user_guid,
                in_tool_use_loop=self._in_tool_use_loop,
                status_callback=status_callback,
                service=service,
            )


# The registry is the one source of tool categories; this view keeps the
# list-valued shape that sibling approval and the debate setup read. The
# knowledge graph tool was never offered by the debate setup, so it stays out.
_TOOL_CATEGORIES: dict[str, list[str]] = {
    category: list(names)
    for category, names in _REGISTRY_CATEGORIES.items()
    if category != "knowledge"
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
