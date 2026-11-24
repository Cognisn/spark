"""Message storage operations."""

from __future__ import annotations

from datetime import datetime, timezone

from spark.database.scope import (
    CONTRIBUTABLE_CONVERSATIONS,
    OWNED_CONVERSATIONS,
    READABLE_CONVERSATIONS,
    Capability,
    require_access,
)
from spark.db.engine import Executor, Row

__all__ = [
    "add_message",
    "apply_compaction",
    "delete_message",
    "get_messages",
    "mark_messages_as_rolled_up",
    "record_rollup",
]


async def add_message(
    db: Executor,
    conversation_id: int,
    role: str,
    content: str,
    token_count: int,
    user_guid: str,
) -> int:
    """Add a message to a conversation the user may send in, and update its totals, in one
    transaction. Returns the message ID. The message records ``user_guid`` as its sender
    (for an assistant or tool message, the participant whose turn it answers).

    Raises :class:`spark.database.scope.NotOwned` when the user is not a member and
    :class:`spark.database.scope.NotPermitted` when they may only read.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
        await require_access(conn, user_guid, conversation_id, Capability.CONTRIBUTE)
        new_id = await conn.fetch_value(
            """INSERT INTO messages
                (conversation_id, role, content, token_count, timestamp, user_guid)
                VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (conversation_id, role, content, token_count, now, user_guid),
        )
        await conn.execute(
            """UPDATE conversations SET total_tokens = total_tokens + ?, last_updated = ?
                WHERE id = ?""",
            (token_count, now, conversation_id),
        )
    return int(new_id)


async def get_messages(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    *,
    include_rolled_up: bool = False,
) -> list[Row]:
    """Messages of a conversation the user may read (none otherwise)."""
    rolled = "" if include_rolled_up else " AND is_rolled_up = 0"
    return await db.fetch_all(
        f"""SELECT * FROM messages WHERE conversation_id = ?
            AND conversation_id IN ({READABLE_CONVERSATIONS}){rolled}
            ORDER BY timestamp""",
        (conversation_id, user_guid),
    )


async def mark_messages_as_rolled_up(
    db: Executor, user_guid: str, conversation_id: int, message_ids: list[int]
) -> None:
    """Mark messages of a conversation the user may send in as rolled up after compaction."""
    if not message_ids:
        return
    placeholders = ", ".join("?" for _ in message_ids)
    await db.execute(
        f"UPDATE messages SET is_rolled_up = 1 "
        f"WHERE conversation_id = ? AND conversation_id IN ({CONTRIBUTABLE_CONVERSATIONS}) "
        f"AND id IN ({placeholders})",
        (conversation_id, user_guid, *message_ids),
    )


async def record_rollup(
    db: Executor,
    conversation_id: int,
    original_message_count: int,
    summarised_content: str,
    original_token_count: int,
    summarised_token_count: int,
    user_guid: str,
) -> None:
    """Record a compaction event of a conversation the user may send in (nothing otherwise).

    The row records ``user_guid``, the participant whose turn compacted it.
    """
    await db.execute(
        f"""INSERT INTO rollup_history
            (conversation_id, original_message_count, summarised_content,
             original_token_count, summarised_token_count, user_guid)
            SELECT id, ?, ?, ?, ?, ? FROM conversations
            WHERE id = ? AND id IN ({CONTRIBUTABLE_CONVERSATIONS})""",
        (
            original_message_count,
            summarised_content,
            original_token_count,
            summarised_token_count,
            user_guid,
            conversation_id,
            user_guid,
        ),
    )


async def delete_message(
    db: Executor, user_guid: str, message_id: int, conversation_id: int
) -> None:
    """Delete a message of a conversation the user owns and adjust its token count, in one
    transaction. Only the owner manages a conversation's history."""
    async with db.transaction() as conn:
        tokens = await conn.fetch_value(
            f"""SELECT token_count FROM messages WHERE id = ? AND conversation_id = ?
                AND conversation_id IN ({OWNED_CONVERSATIONS})""",
            (message_id, conversation_id, user_guid),
        )
        if tokens is None:
            return
        await conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await conn.execute(
            """UPDATE conversations SET total_tokens =
                CASE WHEN total_tokens - ? < 0 THEN 0 ELSE total_tokens - ? END
                WHERE id = ?""",
            (tokens, tokens, conversation_id),
        )


async def apply_compaction(
    db: Executor,
    conversation_id: int,
    marker: str,
    marker_tokens: int,
    rollup_ids: list[int],
    user_guid: str,
    *,
    summary: str | None,
    original_tokens: int | None,
) -> int:
    """Replace rolled-up messages with a marker, in one transaction. Returns the new total.

    Adds the marker message, marks ``rollup_ids`` as rolled up, records the rollup
    when ``summary`` is given, and recalculates the conversation's token total.
    """
    from spark.database import conversations

    async with db.transaction() as conn:
        await add_message(conn, conversation_id, "user", marker, marker_tokens, user_guid)
        await mark_messages_as_rolled_up(conn, user_guid, conversation_id, rollup_ids)
        if summary is not None:
            await record_rollup(
                conn,
                conversation_id,
                len(rollup_ids),
                summary,
                original_tokens or 0,
                marker_tokens,
                user_guid,
            )
        return await conversations.recalculate_total_tokens(conn, user_guid, conversation_id)
