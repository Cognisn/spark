"""Message storage operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def add_message(
    db: DatabaseConnection,
    conversation_id: int,
    role: str,
    content: str,
    token_count: int,
    user_guid: str,
) -> int:
    """Add a message and update conversation totals. Returns message ID."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        f"""INSERT INTO messages (conversation_id, role, content, token_count, timestamp, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (conversation_id, role, content, token_count, now, user_guid),
    )
    # Update conversation total
    db.execute(
        f"""UPDATE conversations SET total_tokens = total_tokens + {ph}, last_updated = {ph}
            WHERE id = {ph}""",
        (token_count, now, conversation_id),
    )
    db.commit()
    return cursor.lastrowid


def get_messages(
    db: DatabaseConnection,
    conversation_id: int,
    *,
    include_rolled_up: bool = False,
) -> list[dict]:
    """Get messages for a conversation."""
    ph = db.placeholder
    if include_rolled_up:
        cursor = db.execute(
            f"SELECT * FROM messages WHERE conversation_id = {ph} ORDER BY timestamp",
            (conversation_id,),
        )
    else:
        cursor = db.execute(
            f"""SELECT * FROM messages WHERE conversation_id = {ph} AND is_rolled_up = 0
                ORDER BY timestamp""",
            (conversation_id,),
        )
    return [dict(row) for row in cursor.fetchall()]


def mark_messages_as_rolled_up(
    db: DatabaseConnection, conversation_id: int, message_ids: list[int]
) -> None:
    """Mark messages as rolled up after compaction."""
    if not message_ids:
        return
    ph = db.placeholder
    placeholders = ", ".join([ph] * len(message_ids))
    db.execute(
        f"UPDATE messages SET is_rolled_up = 1 WHERE conversation_id = {ph} AND id IN ({placeholders})",
        (conversation_id, *message_ids),
    )
    db.commit()


def record_rollup(
    db: DatabaseConnection,
    conversation_id: int,
    original_message_count: int,
    summarised_content: str,
    original_token_count: int,
    summarised_token_count: int,
    user_guid: str,
) -> None:
    """Record a compaction event in rollup history."""
    ph = db.placeholder
    db.execute(
        f"""INSERT INTO rollup_history
            (conversation_id, original_message_count, summarised_content,
             original_token_count, summarised_token_count, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (
            conversation_id,
            original_message_count,
            summarised_content,
            original_token_count,
            summarised_token_count,
            user_guid,
        ),
    )
    db.commit()


def delete_message(db: DatabaseConnection, message_id: int, conversation_id: int) -> None:
    """Delete a message and adjust conversation token count."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT token_count FROM messages WHERE id = {ph} AND conversation_id = {ph}",
        (message_id, conversation_id),
    )
    row = cursor.fetchone()
    if row:
        db.execute(f"DELETE FROM messages WHERE id = {ph}", (message_id,))
        db.execute(
            f"UPDATE conversations SET total_tokens = MAX(0, total_tokens - {ph}) WHERE id = {ph}",
            (row[0], conversation_id),
        )
        db.commit()
