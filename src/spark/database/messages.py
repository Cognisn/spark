"""Message storage operations."""

from __future__ import annotations

from datetime import datetime, timezone

from spark.db.engine import Executor, Row

__all__ = [
    "add_message",
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
    """Add a message and update conversation totals, in one transaction. Returns message ID."""
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
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
    conversation_id: int,
    *,
    include_rolled_up: bool = False,
) -> list[Row]:
    """Get messages for a conversation."""
    if include_rolled_up:
        return await db.fetch_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY timestamp",
            (conversation_id,),
        )
    return await db.fetch_all(
        """SELECT * FROM messages WHERE conversation_id = ? AND is_rolled_up = 0
            ORDER BY timestamp""",
        (conversation_id,),
    )


async def mark_messages_as_rolled_up(
    db: Executor, conversation_id: int, message_ids: list[int]
) -> None:
    """Mark messages as rolled up after compaction."""
    if not message_ids:
        return
    placeholders = ", ".join("?" for _ in message_ids)
    await db.execute(
        f"UPDATE messages SET is_rolled_up = 1 "
        f"WHERE conversation_id = ? AND id IN ({placeholders})",
        (conversation_id, *message_ids),
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
    """Record a compaction event in rollup history."""
    await db.execute(
        """INSERT INTO rollup_history
            (conversation_id, original_message_count, summarised_content,
             original_token_count, summarised_token_count, user_guid)
            VALUES (?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            original_message_count,
            summarised_content,
            original_token_count,
            summarised_token_count,
            user_guid,
        ),
    )


async def delete_message(db: Executor, message_id: int, conversation_id: int) -> None:
    """Delete a message and adjust conversation token count, in one transaction."""
    async with db.transaction() as conn:
        tokens = await conn.fetch_value(
            "SELECT token_count FROM messages WHERE id = ? AND conversation_id = ?",
            (message_id, conversation_id),
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
