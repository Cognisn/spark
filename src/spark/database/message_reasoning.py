"""Reasoning kept beside a message (``message_reasoning``), apart from its content.

A row holds only the readable text a model reasoned in before its reply. It is
removed with its message, and kept as long as the message is.
"""

from __future__ import annotations

from spark.database.scope import CONTRIBUTABLE_CONVERSATIONS, READABLE_CONVERSATIONS
from spark.db.engine import Executor

__all__ = ["add_reasoning", "get_reasoning"]


async def add_reasoning(
    db: Executor, user_guid: str, conversation_id: int, message_id: int, content: str
) -> None:
    """Keep ``content`` as the reasoning of a message of a conversation the user may send in
    (nothing otherwise, or for a message that is not in the conversation)."""
    await db.execute(
        f"""INSERT INTO message_reasoning (message_id, conversation_id, content)
            SELECT id, conversation_id, ? FROM messages
            WHERE id = ? AND conversation_id = ?
            AND conversation_id IN ({CONTRIBUTABLE_CONVERSATIONS})""",
        (content, message_id, conversation_id, user_guid),
    )


async def get_reasoning(db: Executor, user_guid: str, conversation_id: int) -> dict[int, str]:
    """The reasoning of a conversation's messages, by message id (none unless the user may
    read the conversation)."""
    rows = await db.fetch_all(
        f"""SELECT message_id, content FROM message_reasoning
            WHERE conversation_id = ? AND conversation_id IN ({READABLE_CONVERSATIONS})""",
        (conversation_id, user_guid),
    )
    return {int(r["message_id"]): str(r["content"]) for r in rows}
