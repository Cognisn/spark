"""Conversation CRUD operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.db.dialect import upsert
from spark.db.engine import Executor, Row

__all__ = [
    "create_conversation",
    "delete_conversation",
    "get_active_conversations",
    "get_conversation",
    "get_conversation_type",
    "recalculate_total_tokens",
    "search_conversations",
    "update_conversation",
    "update_token_usage",
]


async def create_conversation(
    db: Executor,
    name: str,
    model_id: str,
    user_guid: str,
    *,
    instructions: str | None = None,
    compaction_threshold: float | None = None,
    web_search_enabled: bool = False,
    memory_enabled: bool = True,
) -> int:
    """Create a new conversation and return its ID."""
    now = datetime.now(timezone.utc).isoformat()
    new_id = await db.fetch_value(
        """INSERT INTO conversations
            (name, model_id, created_at, last_updated, instructions,
             compaction_threshold, web_search_enabled, memory_enabled, user_guid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            name,
            model_id,
            now,
            now,
            instructions,
            compaction_threshold,
            int(web_search_enabled),
            int(memory_enabled),
            user_guid,
        ),
    )
    return int(new_id)


async def get_conversation(db: Executor, conversation_id: int, user_guid: str) -> Row | None:
    """Get a single conversation by ID."""
    return await db.fetch_one(
        "SELECT * FROM conversations WHERE id = ? AND user_guid = ?",
        (conversation_id, user_guid),
    )


async def get_active_conversations(db: Executor, user_guid: str) -> list[Row]:
    """Get all active conversations for a user."""
    return await db.fetch_all(
        """SELECT c.*, COUNT(m.id) as message_count
            FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id AND m.is_rolled_up = 0
            WHERE c.is_active = 1 AND c.user_guid = ?
            GROUP BY c.id
            ORDER BY c.last_updated DESC""",
        (user_guid,),
    )


_UPDATABLE = frozenset(
    {
        "name",
        "model_id",
        "total_tokens",
        "is_active",
        "instructions",
        "tokens_sent",
        "tokens_received",
        "max_tokens",
        "compaction_threshold",
        "compaction_model",
        "compaction_summary_ratio",
        "web_search_enabled",
        "memory_enabled",
        "rag_enabled",
        "rag_top_k",
        "rag_threshold",
        "rag_tool_enabled",
        "max_history_messages",
        "include_tool_results",
        "prompt_caching",
        "is_favourite",
        "agents_enabled",
        "agent_mode",
        "agent_model_selection",
        "conversation_type",
        "kg_local_enabled",
        "kg_use_global",
        "kg_auto_context",
    }
)


async def update_conversation(
    db: Executor,
    conversation_id: int,
    user_guid: str,
    **kwargs: Any,
) -> None:
    """Update conversation fields (column names are checked against the schema)."""
    if not kwargs:
        return
    unknown = set(kwargs) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown conversation fields: {', '.join(sorted(unknown))}")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    params = list(kwargs.values()) + [conversation_id, user_guid]
    await db.execute(
        f"UPDATE conversations SET {sets}, last_updated = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_guid = ?",
        tuple(params),
    )


async def delete_conversation(db: Executor, conversation_id: int, user_guid: str) -> None:
    """Soft-delete a conversation."""
    await db.execute(
        "UPDATE conversations SET is_active = 0, last_updated = CURRENT_TIMESTAMP "
        "WHERE id = ? AND user_guid = ?",
        (conversation_id, user_guid),
    )


_MODEL_USAGE = upsert(
    "conversation_model_usage",
    [
        "conversation_id",
        "model_id",
        "input_tokens",
        "output_tokens",
        "first_used",
        "last_used",
        "user_guid",
    ],
    ["conversation_id", "model_id"],
)


async def update_token_usage(
    db: Executor,
    conversation_id: int,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    user_guid: str,
) -> None:
    """Update per-model token usage for a conversation.

    `tokens_sent` and `tokens_received` are lifetime billing counters.
    `total_tokens` is deliberately NOT touched here: it represents the current
    active context size (used by the compaction threshold) and is maintained
    by `messages.add_message` / `delete_message` / `recalculate_total_tokens`.
    """
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE conversations SET
            tokens_sent = tokens_sent + ?,
            tokens_received = tokens_received + ?,
            last_updated = ?
            WHERE id = ?""",
        (input_tokens, output_tokens, now, conversation_id),
    )
    await db.execute(
        _MODEL_USAGE,
        (conversation_id, model_id, input_tokens, output_tokens, now, now, user_guid),
    )


async def recalculate_total_tokens(db: Executor, conversation_id: int) -> int:
    """Recalculate and update total tokens from active messages."""
    total = await db.fetch_value(
        """SELECT COALESCE(SUM(token_count), 0) as total
            FROM messages WHERE conversation_id = ? AND is_rolled_up = 0""",
        (conversation_id,),
    )
    await db.execute(
        "UPDATE conversations SET total_tokens = ? WHERE id = ?",
        (int(total), conversation_id),
    )
    return int(total)


async def search_conversations(db: Executor, query: str, user_guid: str) -> list[Row]:
    """Search conversations by name or message content."""
    pattern = f"%{query}%"
    return await db.fetch_all(
        """SELECT DISTINCT c.* FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE c.is_active = 1 AND c.user_guid = ?
            AND (c.name LIKE ? OR m.content LIKE ?)
            ORDER BY c.last_updated DESC""",
        (user_guid, pattern, pattern),
    )


async def get_conversation_type(db: Executor, conversation_id: int) -> str | None:
    """The conversation's type ('standard', 'debate', 'panel'), or None if it does not exist."""
    value = await db.fetch_value(
        "SELECT conversation_type FROM conversations WHERE id = ?", (conversation_id,)
    )
    return str(value) if value is not None else None
