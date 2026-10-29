"""Conversation link operations: one-directional cross-conversation context sharing."""

from __future__ import annotations

from spark.db.engine import Executor, Row

__all__ = ["add_link", "get_linkable_conversations", "get_links", "remove_link"]


async def add_link(db: Executor, source_id: int, target_id: int, user_guid: str) -> bool:
    """Link source conversation to target (one-directional). Returns True on success.

    A duplicate link, or a link to a conversation that does not exist, is refused.
    """
    if source_id == target_id:
        return False
    try:
        async with db.transaction() as conn:
            new_id = await conn.fetch_value(
                """INSERT INTO conversation_links
                    (source_conversation_id, target_conversation_id, user_guid)
                    VALUES (?, ?, ?)
                    ON CONFLICT (source_conversation_id, target_conversation_id) DO NOTHING
                    RETURNING id""",
                (source_id, target_id, user_guid),
            )
    except Exception:  # foreign key: the conversation does not exist
        return False
    return new_id is not None


async def remove_link(db: Executor, source_id: int, target_id: int, user_guid: str) -> None:
    """Remove a conversation link."""
    await db.execute(
        """DELETE FROM conversation_links
            WHERE source_conversation_id = ?
            AND target_conversation_id = ?
            AND user_guid = ?""",
        (source_id, target_id, user_guid),
    )


async def get_links(db: Executor, source_id: int, user_guid: str) -> list[Row]:
    """Get all conversations linked from the source."""
    return await db.fetch_all(
        """SELECT c.id, c.name, c.model_id, c.created_at
            FROM conversation_links l
            JOIN conversations c ON c.id = l.target_conversation_id
            WHERE l.source_conversation_id = ? AND l.user_guid = ?
            AND c.is_active = 1
            ORDER BY c.name""",
        (source_id, user_guid),
    )


async def get_linkable_conversations(db: Executor, source_id: int, user_guid: str) -> list[Row]:
    """Get active conversations that can be linked to (excluding self and already linked)."""
    return await db.fetch_all(
        """SELECT c.id, c.name, c.model_id, c.created_at
            FROM conversations c
            WHERE c.is_active = 1
            AND c.user_guid = ?
            AND c.id != ?
            AND c.id NOT IN (
                SELECT target_conversation_id FROM conversation_links
                WHERE source_conversation_id = ? AND user_guid = ?
            )
            ORDER BY c.name""",
        (user_guid, source_id, source_id, user_guid),
    )
