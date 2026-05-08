"""Tool permission management."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def check_tool_permission(
    db: DatabaseConnection, conversation_id: int, tool_name: str
) -> str | None:
    """Check tool permission state. Returns 'allowed', 'denied', or None (no record)."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT permission_state FROM conversation_tool_permissions
            WHERE conversation_id = {ph} AND tool_name = {ph}""",
        (conversation_id, tool_name),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_tool_permission(
    db: DatabaseConnection,
    conversation_id: int,
    tool_name: str,
    permission_state: str,
    user_guid: str,
) -> None:
    """Set or update a tool permission."""
    upsert = db.backend.upsert_sql(
        "conversation_tool_permissions",
        ["conversation_id", "tool_name", "permission_state", "user_guid"],
        ["conversation_id", "tool_name"],
    )
    db.execute(upsert, (conversation_id, tool_name, permission_state, user_guid))
    db.commit()


def get_all_tool_permissions(db: DatabaseConnection, conversation_id: int) -> list[dict]:
    """Get all tool permissions for a conversation."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM conversation_tool_permissions WHERE conversation_id = {ph}",
        (conversation_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def is_tool_allowed(
    db: DatabaseConnection,
    conversation_id: int,
    tool_name: str,
    user_guid: str = "",
) -> bool | None:
    """Returns True if allowed, False if denied, None if no record (prompt user).

    Checks conversation-level permissions first, then global permissions.
    """
    state = check_tool_permission(db, conversation_id, tool_name)
    if state is not None:
        return state == "allowed"

    # Fall back to global permissions
    if user_guid:
        global_state = check_global_tool_permission(db, user_guid, tool_name)
        if global_state is not None:
            return global_state == "allowed"

    return None


def delete_tool_permission(db: DatabaseConnection, conversation_id: int, tool_name: str) -> None:
    """Delete a tool permission (resets to prompt-on-first-use)."""
    ph = db.placeholder
    db.execute(
        f"""DELETE FROM conversation_tool_permissions
            WHERE conversation_id = {ph} AND tool_name = {ph}""",
        (conversation_id, tool_name),
    )
    db.commit()


# -- Global tool permissions --------------------------------------------------


def check_global_tool_permission(
    db: DatabaseConnection, user_guid: str, tool_name: str
) -> str | None:
    """Check global tool permission. Returns 'allowed', 'denied', or None."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT permission_state FROM global_tool_permissions
            WHERE user_guid = {ph} AND tool_name = {ph}""",
        (user_guid, tool_name),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_global_tool_permission(
    db: DatabaseConnection,
    user_guid: str,
    tool_name: str,
    permission_state: str,
) -> None:
    """Set or update a global tool permission."""
    upsert = db.backend.upsert_sql(
        "global_tool_permissions",
        ["user_guid", "tool_name", "permission_state"],
        ["user_guid", "tool_name"],
    )
    db.execute(upsert, (user_guid, tool_name, permission_state))
    db.commit()


def get_all_global_tool_permissions(db: DatabaseConnection, user_guid: str) -> list[dict]:
    """Get all global tool permissions for a user."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM global_tool_permissions WHERE user_guid = {ph}",
        (user_guid,),
    )
    return [dict(row) for row in cursor.fetchall()]


def delete_global_tool_permission(db: DatabaseConnection, user_guid: str, tool_name: str) -> None:
    """Delete a global tool permission."""
    ph = db.placeholder
    db.execute(
        f"""DELETE FROM global_tool_permissions
            WHERE user_guid = {ph} AND tool_name = {ph}""",
        (user_guid, tool_name),
    )
    db.commit()
