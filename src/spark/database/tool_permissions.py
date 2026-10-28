"""Tool permission management."""

from __future__ import annotations

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
    ["conversation_id", "tool_name"],
)
_GLOBAL_UPSERT = upsert(
    "global_tool_permissions",
    ["user_guid", "tool_name", "permission_state"],
    ["user_guid", "tool_name"],
)


async def check_tool_permission(db: Executor, conversation_id: int, tool_name: str) -> str | None:
    """Check tool permission state. Returns 'allowed', 'denied', or None (no record)."""
    state = await db.fetch_value(
        """SELECT permission_state FROM conversation_tool_permissions
            WHERE conversation_id = ? AND tool_name = ?""",
        (conversation_id, tool_name),
    )
    return str(state) if state is not None else None


async def set_tool_permission(
    db: Executor,
    conversation_id: int,
    tool_name: str,
    permission_state: str,
    user_guid: str,
) -> None:
    """Set or update a tool permission."""
    await db.execute(
        _CONVERSATION_UPSERT, (conversation_id, tool_name, permission_state, user_guid)
    )


async def get_all_tool_permissions(db: Executor, conversation_id: int) -> list[Row]:
    """Get all tool permissions for a conversation."""
    return await db.fetch_all(
        "SELECT * FROM conversation_tool_permissions WHERE conversation_id = ?",
        (conversation_id,),
    )


async def is_tool_allowed(
    db: Executor,
    conversation_id: int,
    tool_name: str,
    user_guid: str = "",
) -> bool | None:
    """Returns True if allowed, False if denied, None if no record (prompt user).

    Checks conversation-level permissions first, then global permissions.
    """
    state = await check_tool_permission(db, conversation_id, tool_name)
    if state is not None:
        return state == "allowed"

    if user_guid:
        global_state = await check_global_tool_permission(db, user_guid, tool_name)
        if global_state is not None:
            return global_state == "allowed"

    return None


async def delete_tool_permission(db: Executor, conversation_id: int, tool_name: str) -> None:
    """Delete a tool permission (resets to prompt-on-first-use)."""
    await db.execute(
        """DELETE FROM conversation_tool_permissions
            WHERE conversation_id = ? AND tool_name = ?""",
        (conversation_id, tool_name),
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
