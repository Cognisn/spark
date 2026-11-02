"""MCP transaction tracking and per-conversation tool state, scoped by the
conversation's user."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.database.scope import OWNED_CONVERSATIONS, NotOwned, owns_conversation
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
    """Record a tool transaction in one of the user's conversations. Returns its ID.

    Raises :class:`NotOwned` when the conversation is not the user's.
    """
    now = datetime.now(timezone.utc).isoformat()
    new_id = await db.fetch_value(
        """INSERT INTO mcp_transactions
            (conversation_id, message_id, user_prompt, tool_name, tool_server,
             tool_input, tool_response, is_error, execution_time_ms,
             transaction_timestamp, user_guid)
            SELECT id, ?, ?, ?, ?, ?, ?, ?, ?, ?, user_guid FROM conversations
            WHERE id = ? AND user_guid = ?
            RETURNING id""",
        (
            message_id,
            user_prompt,
            tool_name,
            tool_server,
            tool_input,
            tool_response,
            int(is_error),
            execution_time_ms,
            now,
            conversation_id,
            user_guid,
        ),
    )
    if new_id is None:
        raise NotOwned(f"conversation {conversation_id} not found")
    return int(new_id)


async def get_transactions(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    *,
    limit: int = 100,
    tool_name: str | None = None,
) -> list[Row]:
    """Tool transactions of one of the user's conversations, newest first."""
    sql = (
        "SELECT * FROM mcp_transactions WHERE conversation_id = ? "
        f"AND conversation_id IN ({OWNED_CONVERSATIONS})"
    )
    params: list[Any] = [conversation_id, user_guid]
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
    """Set an MCP server's enabled state for one of the user's conversations."""
    async with db.transaction() as conn:
        if await owns_conversation(conn, user_guid, conversation_id):
            await conn.execute(
                _SERVER_UPSERT, (conversation_id, server_name, int(enabled), user_guid)
            )


async def is_mcp_server_enabled(
    db: Executor, user_guid: str, conversation_id: int, server_name: str
) -> bool:
    """Whether an MCP server is enabled for one of the user's conversations. Default: True."""
    enabled = await db.fetch_value(
        f"""SELECT enabled FROM conversation_mcp_servers
            WHERE conversation_id = ? AND server_name = ?
            AND conversation_id IN ({OWNED_CONVERSATIONS})""",
        (conversation_id, server_name, user_guid),
    )
    return bool(enabled) if enabled is not None else True


async def set_embedded_tool_enabled(
    db: Executor,
    conversation_id: int,
    tool_name: str,
    enabled: bool,
    user_guid: str,
) -> None:
    """Set a tool's enabled state for one of the user's conversations."""
    async with db.transaction() as conn:
        if await owns_conversation(conn, user_guid, conversation_id):
            await conn.execute(
                _EMBEDDED_UPSERT, (conversation_id, tool_name, int(enabled), user_guid)
            )


async def is_embedded_tool_enabled(
    db: Executor, user_guid: str, conversation_id: int, tool_name: str
) -> bool:
    """Whether a tool is enabled for one of the user's conversations. Default: True."""
    enabled = await db.fetch_value(
        f"""SELECT enabled FROM conversation_embedded_tools
            WHERE conversation_id = ? AND tool_name = ? AND user_guid = ?
            AND conversation_id IN ({OWNED_CONVERSATIONS})""",
        (conversation_id, tool_name, user_guid, user_guid),
    )
    return bool(enabled) if enabled is not None else True
