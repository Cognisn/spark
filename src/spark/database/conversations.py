"""Conversation CRUD operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.database.scope import (
    OWNED_CONVERSATIONS,
    READABLE_CONVERSATIONS,
    Capability,
    allows,
    conversation_role,
)
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
    """Create a new conversation owned by ``user_guid`` and return its ID.

    The owner's member row is written in the same transaction, so it is always in
    step with ``conversations.user_guid``.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
        new_id = await conn.fetch_value(
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
        await conn.execute(
            """INSERT INTO conversation_members
                (conversation_id, user_guid, role, added_by, added_at, last_seen_at)
                VALUES (?, ?, 'owner', ?, ?, ?)""",
            (int(new_id), user_guid, user_guid, now, now),
        )
    return int(new_id)


async def get_conversation(db: Executor, conversation_id: int, user_guid: str) -> Row | None:
    """A conversation ``user_guid`` may read (owner or member), or None."""
    return await db.fetch_one(
        f"SELECT * FROM conversations WHERE id = ? AND id IN ({READABLE_CONVERSATIONS})",
        (conversation_id, user_guid),
    )


async def get_active_conversations(db: Executor, user_guid: str) -> list[Row]:
    """The user's own active conversations (those they own), newest first.

    ``is_shared`` is 1 for a conversation the owner has shared with anyone. The
    conversations shared with the user are listed by
    :func:`spark.database.conversation_members.shared_with`.
    """
    return await db.fetch_all(
        f"""SELECT c.*, COUNT(m.id) as message_count,
                CASE WHEN EXISTS (SELECT 1 FROM conversation_members cm
                    WHERE cm.conversation_id = c.id AND cm.role <> 'owner')
                THEN 1 ELSE 0 END AS is_shared
            FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id AND m.is_rolled_up = 0
            WHERE c.is_active = 1 AND c.id IN ({OWNED_CONVERSATIONS})
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
    """Update fields of a conversation ``user_guid`` owns (column names are checked against
    the schema). Only the owner manages a conversation: nothing changes for anyone else."""
    if not kwargs:
        return
    unknown = set(kwargs) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown conversation fields: {', '.join(sorted(unknown))}")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    params = list(kwargs.values()) + [conversation_id, user_guid]
    await db.execute(
        f"UPDATE conversations SET {sets}, last_updated = CURRENT_TIMESTAMP "
        f"WHERE id = ? AND id IN ({OWNED_CONVERSATIONS})",
        tuple(params),
    )


async def delete_conversation(db: Executor, conversation_id: int, user_guid: str) -> None:
    """Soft-delete a conversation ``user_guid`` owns (nothing for anyone else)."""
    await db.execute(
        "UPDATE conversations SET is_active = 0, last_updated = CURRENT_TIMESTAMP "
        f"WHERE id = ? AND id IN ({OWNED_CONVERSATIONS})",
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
    """Update per-model token usage of a conversation the user may send in (nothing otherwise).

    `tokens_sent` and `tokens_received` are lifetime billing counters.
    `total_tokens` is deliberately NOT touched here: it represents the current
    active context size (used by the compaction threshold) and is maintained
    by `messages.add_message` / `delete_message` / `recalculate_total_tokens`.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
        role = await conversation_role(conn, user_guid, conversation_id)
        if not allows(role, Capability.CONTRIBUTE):
            return
        await conn.execute(
            """UPDATE conversations SET
                tokens_sent = tokens_sent + ?,
                tokens_received = tokens_received + ?,
                last_updated = ?
                WHERE id = ?""",
            (input_tokens, output_tokens, now, conversation_id),
        )
        await conn.execute(
            _MODEL_USAGE,
            (conversation_id, model_id, input_tokens, output_tokens, now, now, user_guid),
        )


async def recalculate_total_tokens(db: Executor, user_guid: str, conversation_id: int) -> int:
    """Recalculate the total tokens of a conversation the user may send in, from its
    active messages.

    Returns 0, changing nothing, for a conversation the user may not send in.
    """
    async with db.transaction() as conn:
        role = await conversation_role(conn, user_guid, conversation_id)
        if not allows(role, Capability.CONTRIBUTE):
            return 0
        total = await conn.fetch_value(
            """SELECT COALESCE(SUM(token_count), 0) as total
                FROM messages WHERE conversation_id = ? AND is_rolled_up = 0""",
            (conversation_id,),
        )
        await conn.execute(
            "UPDATE conversations SET total_tokens = ? WHERE id = ?",
            (int(total), conversation_id),
        )
    return int(total)


async def search_conversations(db: Executor, query: str, user_guid: str) -> list[Row]:
    """Search the conversations the user may read (own and shared), by name or content."""
    pattern = f"%{query}%"
    return await db.fetch_all(
        f"""SELECT DISTINCT c.* FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE c.is_active = 1 AND c.id IN ({READABLE_CONVERSATIONS})
            AND (c.name LIKE ? OR m.content LIKE ?)
            ORDER BY c.last_updated DESC""",
        (user_guid, pattern, pattern),
    )


async def get_conversation_type(db: Executor, user_guid: str, conversation_id: int) -> str | None:
    """The type ('standard', 'debate', 'panel') of a conversation the user may read.

    None when it does not exist or the user is not a member.
    """
    value = await db.fetch_value(
        "SELECT conversation_type FROM conversations WHERE id = ? "
        f"AND id IN ({READABLE_CONVERSATIONS})",
        (conversation_id, user_guid),
    )
    return str(value) if value is not None else None
