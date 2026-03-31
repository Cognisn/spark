"""Conversation link operations — one-directional cross-conversation context sharing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def add_link(
    db: DatabaseConnection, source_id: int, target_id: int, user_guid: str
) -> bool:
    """Link source conversation to target (one-directional). Returns True on success."""
    if source_id == target_id:
        return False
    ph = db.placeholder
    try:
        db.execute(
            f"""INSERT INTO conversation_links
                (source_conversation_id, target_conversation_id, user_guid)
                VALUES ({ph}, {ph}, {ph})""",
            (source_id, target_id, user_guid),
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def remove_link(
    db: DatabaseConnection, source_id: int, target_id: int, user_guid: str
) -> None:
    """Remove a conversation link."""
    ph = db.placeholder
    db.execute(
        f"""DELETE FROM conversation_links
            WHERE source_conversation_id = {ph}
            AND target_conversation_id = {ph}
            AND user_guid = {ph}""",
        (source_id, target_id, user_guid),
    )
    db.commit()


def get_links(db: DatabaseConnection, source_id: int, user_guid: str) -> list[dict]:
    """Get all conversations linked from the source."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT c.id, c.name, c.model_id, c.created_at
            FROM conversation_links l
            JOIN conversations c ON c.id = l.target_conversation_id
            WHERE l.source_conversation_id = {ph} AND l.user_guid = {ph}
            AND c.is_active = 1
            ORDER BY c.name""",
        (source_id, user_guid),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_linkable_conversations(
    db: DatabaseConnection, source_id: int, user_guid: str
) -> list[dict]:
    """Get active conversations that can be linked to (excluding self and already linked)."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT c.id, c.name, c.model_id, c.created_at
            FROM conversations c
            WHERE c.is_active = 1
            AND c.user_guid = {ph}
            AND c.id != {ph}
            AND c.id NOT IN (
                SELECT target_conversation_id FROM conversation_links
                WHERE source_conversation_id = {ph} AND user_guid = {ph}
            )
            ORDER BY c.name""",
        (user_guid, source_id, source_id, user_guid),
    )
    return [dict(row) for row in cursor.fetchall()]
