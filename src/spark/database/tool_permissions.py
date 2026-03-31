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


def get_all_tool_permissions(
    db: DatabaseConnection, conversation_id: int
) -> list[dict]:
    """Get all tool permissions for a conversation."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM conversation_tool_permissions WHERE conversation_id = {ph}",
        (conversation_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def is_tool_allowed(
    db: DatabaseConnection, conversation_id: int, tool_name: str
) -> bool | None:
    """Returns True if allowed, False if denied, None if no record (prompt user)."""
    state = check_tool_permission(db, conversation_id, tool_name)
    if state is None:
        return None
    return state == "allowed"


def delete_tool_permission(
    db: DatabaseConnection, conversation_id: int, tool_name: str
) -> None:
    """Delete a tool permission (resets to prompt-on-first-use)."""
    ph = db.placeholder
    db.execute(
        f"""DELETE FROM conversation_tool_permissions
            WHERE conversation_id = {ph} AND tool_name = {ph}""",
        (conversation_id, tool_name),
    )
    db.commit()
