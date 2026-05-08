"""Conversation CRUD operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def create_conversation(
    db: DatabaseConnection,
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
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        f"""INSERT INTO conversations
            (name, model_id, created_at, last_updated, instructions,
             compaction_threshold, web_search_enabled, memory_enabled, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
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
    db.commit()
    return cursor.lastrowid


def get_conversation(db: DatabaseConnection, conversation_id: int, user_guid: str) -> dict | None:
    """Get a single conversation by ID."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM conversations WHERE id = {ph} AND user_guid = {ph}",
        (conversation_id, user_guid),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def get_active_conversations(db: DatabaseConnection, user_guid: str) -> list[dict]:
    """Get all active conversations for a user."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT c.*, COUNT(m.id) as message_count
            FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id AND m.is_rolled_up = 0
            WHERE c.is_active = 1 AND c.user_guid = {ph}
            GROUP BY c.id
            ORDER BY c.last_updated DESC""",
        (user_guid,),
    )
    return [dict(row) for row in cursor.fetchall()]


def update_conversation(
    db: DatabaseConnection,
    conversation_id: int,
    user_guid: str,
    **kwargs: Any,
) -> None:
    """Update conversation fields."""
    if not kwargs:
        return
    ph = db.placeholder
    sets = ", ".join(f"{k} = {ph}" for k in kwargs)
    params = list(kwargs.values()) + [conversation_id, user_guid]
    db.execute(
        f"UPDATE conversations SET {sets}, last_updated = CURRENT_TIMESTAMP "
        f"WHERE id = {ph} AND user_guid = {ph}",
        tuple(params),
    )
    db.commit()


def delete_conversation(db: DatabaseConnection, conversation_id: int, user_guid: str) -> None:
    """Soft-delete a conversation."""
    ph = db.placeholder
    db.execute(
        f"UPDATE conversations SET is_active = 0, last_updated = CURRENT_TIMESTAMP "
        f"WHERE id = {ph} AND user_guid = {ph}",
        (conversation_id, user_guid),
    )
    db.commit()


def update_token_usage(
    db: DatabaseConnection,
    conversation_id: int,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    user_guid: str,
) -> None:
    """Update per-model token usage for a conversation.

    `tokens_sent` and `tokens_received` are lifetime billing counters.
    `total_tokens` is deliberately NOT touched here — it represents the current
    active context size (used by the compaction threshold) and is maintained
    by `messages.add_message` / `delete_message` / `recalculate_total_tokens`.
    """
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        f"""UPDATE conversations SET
            tokens_sent = tokens_sent + {ph},
            tokens_received = tokens_received + {ph},
            last_updated = {ph}
            WHERE id = {ph}""",
        (input_tokens, output_tokens, now, conversation_id),
    )

    # Upsert model usage
    upsert = db.backend.upsert_sql(
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
    db.execute(
        upsert, (conversation_id, model_id, input_tokens, output_tokens, now, now, user_guid)
    )
    db.commit()


def recalculate_total_tokens(db: DatabaseConnection, conversation_id: int) -> int:
    """Recalculate and update total tokens from active messages."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT COALESCE(SUM(token_count), 0) as total
            FROM messages WHERE conversation_id = {ph} AND is_rolled_up = 0""",
        (conversation_id,),
    )
    total = cursor.fetchone()[0]
    db.execute(
        f"UPDATE conversations SET total_tokens = {ph} WHERE id = {ph}",
        (total, conversation_id),
    )
    db.commit()
    return total


def search_conversations(db: DatabaseConnection, query: str, user_guid: str) -> list[dict]:
    """Search conversations by name or message content."""
    ph = db.placeholder
    pattern = f"%{query}%"
    cursor = db.execute(
        f"""SELECT DISTINCT c.* FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE c.is_active = 1 AND c.user_guid = {ph}
            AND (c.name LIKE {ph} OR m.content LIKE {ph})
            ORDER BY c.last_updated DESC""",
        (user_guid, pattern, pattern),
    )
    return [dict(row) for row in cursor.fetchall()]
