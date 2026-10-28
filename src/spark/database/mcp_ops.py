"""MCP transaction tracking and server state management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.db.dialect import upsert
from spark.db.engine import Executor, Row

__all__ = [
    "get_transactions",
    "is_embedded_tool_enabled",
    "is_mcp_server_enabled",
    "record_transaction",
    "set_embedded_tool_enabled",
    "set_mcp_server_enabled",
]

_SERVER_UPSERT = upsert(
    "conversation_mcp_servers",
    ["conversation_id", "server_name", "enabled", "user_guid"],
    ["conversation_id", "server_name"],
)
_EMBEDDED_UPSERT = upsert(
    "conversation_embedded_tools",
    ["conversation_id", "tool_name", "enabled", "user_guid"],
    ["conversation_id", "tool_name", "user_guid"],
)


async def record_transaction(
    db: Executor,
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
    now = datetime.now(timezone.utc).isoformat()
    new_id = await db.fetch_value(
        """INSERT INTO mcp_transactions
            (conversation_id, message_id, user_prompt, tool_name, tool_server,
             tool_input, tool_response, is_error, execution_time_ms,
             transaction_timestamp, user_guid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
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
    return int(new_id)


async def get_transactions(
    db: Executor,
    conversation_id: int,
    *,
    limit: int = 100,
    tool_name: str | None = None,
) -> list[Row]:
    """Get MCP transactions for a conversation."""
    sql = "SELECT * FROM mcp_transactions WHERE conversation_id = ?"
    params: list[Any] = [conversation_id]
    if tool_name:
        sql += " AND tool_name = ?"
        params.append(tool_name)
    sql += " ORDER BY transaction_timestamp DESC LIMIT ?"
    params.append(limit)
    return await db.fetch_all(sql, tuple(params))


async def set_mcp_server_enabled(
    db: Executor,
    conversation_id: int,
    server_name: str,
    enabled: bool,
    user_guid: str,
) -> None:
    """Set MCP server enabled state for a conversation."""
    await db.execute(_SERVER_UPSERT, (conversation_id, server_name, int(enabled), user_guid))


async def is_mcp_server_enabled(db: Executor, conversation_id: int, server_name: str) -> bool:
    """Check if an MCP server is enabled for a conversation. Default: True."""
    enabled = await db.fetch_value(
        """SELECT enabled FROM conversation_mcp_servers
            WHERE conversation_id = ? AND server_name = ?""",
        (conversation_id, server_name),
    )
    return bool(enabled) if enabled is not None else True


async def set_embedded_tool_enabled(
    db: Executor,
    conversation_id: int,
    tool_name: str,
    enabled: bool,
    user_guid: str,
) -> None:
    """Set embedded tool enabled state for a conversation."""
    await db.execute(_EMBEDDED_UPSERT, (conversation_id, tool_name, int(enabled), user_guid))


async def is_embedded_tool_enabled(db: Executor, conversation_id: int, tool_name: str) -> bool:
    """Check if an embedded tool is enabled. Default: True."""
    enabled = await db.fetch_value(
        """SELECT enabled FROM conversation_embedded_tools
            WHERE conversation_id = ? AND tool_name = ?""",
        (conversation_id, tool_name),
    )
    return bool(enabled) if enabled is not None else True
