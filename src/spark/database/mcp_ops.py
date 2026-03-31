"""MCP transaction tracking and server state management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def record_transaction(
    db: DatabaseConnection,
    conversation_id: int,
    tool_name: str,
    tool_input: str,
    tool_response: str,
    user_guid: str,
    *,
    message_id: int | None = None,
    user_prompt: str | None = None,
    tool_server: str | None = None,
    is_error: bool = False,
    execution_time_ms: int = 0,
) -> int:
    """Record an MCP tool transaction. Returns transaction ID."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        f"""INSERT INTO mcp_transactions
            (conversation_id, message_id, user_prompt, tool_name, tool_server,
             tool_input, tool_response, is_error, execution_time_ms,
             transaction_timestamp, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (
            conversation_id,
            message_id,
            user_prompt,
            tool_name,
            tool_server,
            tool_input,
            tool_response,
            int(is_error),
            execution_time_ms,
            now,
            user_guid,
        ),
    )
    db.commit()
    return cursor.lastrowid


def get_transactions(
    db: DatabaseConnection,
    conversation_id: int,
    *,
    limit: int = 100,
    tool_name: str | None = None,
) -> list[dict]:
    """Get MCP transactions for a conversation."""
    ph = db.placeholder
    sql = f"SELECT * FROM mcp_transactions WHERE conversation_id = {ph}"
    params: list = [conversation_id]

    if tool_name:
        sql += f" AND tool_name = {ph}"
        params.append(tool_name)

    sql += f" ORDER BY transaction_timestamp DESC LIMIT {ph}"
    params.append(limit)

    cursor = db.execute(sql, tuple(params))
    return [dict(row) for row in cursor.fetchall()]


def set_mcp_server_enabled(
    db: DatabaseConnection,
    conversation_id: int,
    server_name: str,
    enabled: bool,
    user_guid: str,
) -> None:
    """Set MCP server enabled state for a conversation."""
    upsert = db.backend.upsert_sql(
        "conversation_mcp_servers",
        ["conversation_id", "server_name", "enabled", "user_guid"],
        ["conversation_id", "server_name"],
    )
    db.execute(upsert, (conversation_id, server_name, int(enabled), user_guid))
    db.commit()


def is_mcp_server_enabled(db: DatabaseConnection, conversation_id: int, server_name: str) -> bool:
    """Check if an MCP server is enabled for a conversation. Default: True."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT enabled FROM conversation_mcp_servers
            WHERE conversation_id = {ph} AND server_name = {ph}""",
        (conversation_id, server_name),
    )
    row = cursor.fetchone()
    return bool(row[0]) if row else True


def set_embedded_tool_enabled(
    db: DatabaseConnection,
    conversation_id: int,
    tool_name: str,
    enabled: bool,
    user_guid: str,
) -> None:
    """Set embedded tool enabled state for a conversation."""
    upsert = db.backend.upsert_sql(
        "conversation_embedded_tools",
        ["conversation_id", "tool_name", "enabled", "user_guid"],
        ["conversation_id", "tool_name", "user_guid"],
    )
    db.execute(upsert, (conversation_id, tool_name, int(enabled), user_guid))
    db.commit()


def is_embedded_tool_enabled(db: DatabaseConnection, conversation_id: int, tool_name: str) -> bool:
    """Check if an embedded tool is enabled. Default: True."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT enabled FROM conversation_embedded_tools
            WHERE conversation_id = {ph} AND tool_name = {ph}""",
        (conversation_id, tool_name),
    )
    row = cursor.fetchone()
    return bool(row[0]) if row else True
