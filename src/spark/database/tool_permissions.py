"""Tool permission management: per conversation and participant, and global per user.

A permission on a conversation belongs to the participant who answered the prompt:
in a shared conversation one participant's "allow in this conversation" never answers
for another's turn. A participant reaches their own rows only, in a conversation they
may send in.
"""

from __future__ import annotations

from spark.database.scope import (
    CONTRIBUTABLE_CONVERSATIONS,
    Capability,
    allows,
    conversation_role,
)
from spark.db.dialect import upsert
from spark.db.engine import Executor, Row

__all__ = [
    "check_global_tool_permission",
    "check_tool_permission",
    "delete_global_tool_permission",
    "delete_tool_permission",
    "get_all_global_tool_permissions",
    "get_all_tool_permissions",
    "is_tool_allowed",
    "set_global_tool_permission",
    "set_tool_permission",
]

_CONVERSATION_UPSERT = upsert(
    "conversation_tool_permissions",
    ["conversation_id", "tool_name", "permission_state", "user_guid"],
    ["conversation_id", "user_guid", "tool_name"],
)
_GLOBAL_UPSERT = upsert(
    "global_tool_permissions",
    ["user_guid", "tool_name", "permission_state"],
    ["user_guid", "tool_name"],
)


async def check_tool_permission(
    db: Executor, user_guid: str, conversation_id: int, tool_name: str
) -> str | None:
    """The user's own state for a tool in a conversation: 'allowed', 'denied' or None."""
    state = await db.fetch_value(
        f"""SELECT permission_state FROM conversation_tool_permissions
            WHERE conversation_id = ? AND tool_name = ? AND user_guid = ?
            AND conversation_id IN ({CONTRIBUTABLE_CONVERSATIONS})""",
        (conversation_id, tool_name, user_guid, user_guid),
    )
    return str(state) if state is not None else None


async def set_tool_permission(
    db: Executor,
    conversation_id: int,
    tool_name: str,
    permission_state: str,
    user_guid: str,
) -> None:
    """Set or update the user's own tool permission in a conversation they may send in."""
    async with db.transaction() as conn:
        role = await conversation_role(conn, user_guid, conversation_id)
        if allows(role, Capability.CONTRIBUTE):
            await conn.execute(
                _CONVERSATION_UPSERT, (conversation_id, tool_name, permission_state, user_guid)
            )


async def get_all_tool_permissions(db: Executor, user_guid: str, conversation_id: int) -> list[Row]:
    """The user's own tool permissions in a conversation they may send in."""
    return await db.fetch_all(
        f"""SELECT * FROM conversation_tool_permissions WHERE conversation_id = ?
            AND user_guid = ? AND conversation_id IN ({CONTRIBUTABLE_CONVERSATIONS})""",
        (conversation_id, user_guid, user_guid),
    )


async def is_tool_allowed(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    tool_name: str,
) -> bool | None:
    """True if allowed, False if denied, None if no record (prompt the user).

    Checks the user's own permission in the conversation first, then the user's
    global permission.
    """
    state = await check_tool_permission(db, user_guid, conversation_id, tool_name)
    if state is not None:
        return state == "allowed"

    global_state = await check_global_tool_permission(db, user_guid, tool_name)
    if global_state is not None:
        return global_state == "allowed"

    return None


async def delete_tool_permission(
    db: Executor, user_guid: str, conversation_id: int, tool_name: str
) -> None:
    """Delete the user's own tool permission in a conversation (prompt on next use)."""
    await db.execute(
        f"""DELETE FROM conversation_tool_permissions
            WHERE conversation_id = ? AND tool_name = ? AND user_guid = ?
            AND conversation_id IN ({CONTRIBUTABLE_CONVERSATIONS})""",
        (conversation_id, tool_name, user_guid, user_guid),
    )


# -- Global tool permissions --------------------------------------------------


async def check_global_tool_permission(db: Executor, user_guid: str, tool_name: str) -> str | None:
    """Check global tool permission. Returns 'allowed', 'denied', or None."""
    state = await db.fetch_value(
        """SELECT permission_state FROM global_tool_permissions
            WHERE user_guid = ? AND tool_name = ?""",
        (user_guid, tool_name),
    )
    return str(state) if state is not None else None


async def set_global_tool_permission(
    db: Executor,
    user_guid: str,
    tool_name: str,
    permission_state: str,
) -> None:
    """Set or update a global tool permission."""
    await db.execute(_GLOBAL_UPSERT, (user_guid, tool_name, permission_state))


async def get_all_global_tool_permissions(db: Executor, user_guid: str) -> list[Row]:
    """Get all global tool permissions for a user."""
    return await db.fetch_all(
        "SELECT * FROM global_tool_permissions WHERE user_guid = ?", (user_guid,)
    )


async def delete_global_tool_permission(db: Executor, user_guid: str, tool_name: str) -> None:
    """Delete a global tool permission."""
    await db.execute(
        """DELETE FROM global_tool_permissions
            WHERE user_guid = ? AND tool_name = ?""",
        (user_guid, tool_name),
    )
