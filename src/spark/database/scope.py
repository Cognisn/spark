"""User scoping shared by the repositories.

Every row a user reaches through a parent id (a conversation's messages, an
action's runs, a debate turn's exhibits) belongs to the user who owns the
parent. Repositories add one of the subqueries below to such a query, so a
caller holding another user's id gets nothing back and changes nothing.

``OWNED_CONVERSATIONS`` and ``OWNED_ACTIONS`` each take one parameter, the
caller's user guid.
"""

from __future__ import annotations

from spark.db.engine import Executor

__all__ = [
    "OWNED_ACTIONS",
    "OWNED_CONVERSATIONS",
    "NotOwned",
    "owns_action",
    "owns_conversation",
    "require_conversation",
]

OWNED_CONVERSATIONS = "SELECT id FROM conversations WHERE user_guid = ?"
OWNED_ACTIONS = "SELECT id FROM autonomous_actions WHERE user_guid = ?"


class NotOwned(LookupError):
    """The parent does not exist for this user (it is missing or another user's)."""


async def owns_conversation(db: Executor, user_guid: str, conversation_id: int) -> bool:
    """Whether ``conversation_id`` is one of ``user_guid``'s conversations."""
    found = await db.fetch_value(
        "SELECT 1 FROM conversations WHERE id = ? AND user_guid = ?",
        (conversation_id, user_guid),
    )
    return found is not None


async def owns_action(db: Executor, user_guid: str, action_id: int) -> bool:
    """Whether ``action_id`` is one of ``user_guid``'s actions."""
    found = await db.fetch_value(
        "SELECT 1 FROM autonomous_actions WHERE id = ? AND user_guid = ?",
        (action_id, user_guid),
    )
    return found is not None


async def require_conversation(db: Executor, user_guid: str, conversation_id: int) -> None:
    """Raise :class:`NotOwned` unless ``conversation_id`` is ``user_guid``'s."""
    if not await owns_conversation(db, user_guid, conversation_id):
        raise NotOwned(f"conversation {conversation_id} not found")
