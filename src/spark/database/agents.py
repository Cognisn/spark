"""Agent run tracking, scoped through the parent conversation's user."""

from __future__ import annotations

from datetime import datetime, timezone

from spark.database.scope import OWNED_CONVERSATIONS, NotOwned
from spark.db.engine import Executor, Row

__all__ = ["complete_agent_run", "create_agent_run", "get_agent_run", "get_agent_runs"]


async def create_agent_run(
    db: Executor,
    agent_id: str,
    parent_conversation_id: int,
    agent_name: str,
    task_description: str,
    mode: str,
    model_id: str,
    user_guid: str,
) -> int:
    """Record a new agent run under one of the user's conversations. Returns the row ID.

    Raises :class:`NotOwned` when the conversation is not the user's.
    """
    new_id = await db.fetch_value(
        """INSERT INTO agent_runs
            (agent_id, parent_conversation_id, agent_name, task_description,
             mode, model_id, user_guid)
            SELECT ?, id, ?, ?, ?, ?, user_guid FROM conversations
            WHERE id = ? AND user_guid = ?
            RETURNING id""",
        (
            agent_id,
            agent_name,
            task_description,
            mode,
            model_id,
            parent_conversation_id,
            user_guid,
        ),
    )
    if new_id is None:
        raise NotOwned(f"conversation {parent_conversation_id} not found")
    return int(new_id)


async def complete_agent_run(
    db: Executor,
    user_guid: str,
    agent_id: str,
    *,
    status: str = "completed",
    result_text: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls_json: str | None = None,
) -> None:
    """Mark one of the user's agent runs as complete."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        f"""UPDATE agent_runs SET status = ?, result_text = ?,
            input_tokens = ?, output_tokens = ?,
            tool_calls_json = ?, completed_at = ?
            WHERE agent_id = ? AND parent_conversation_id IN ({OWNED_CONVERSATIONS})""",
        (
            status,
            result_text,
            input_tokens,
            output_tokens,
            tool_calls_json,
            now,
            agent_id,
            user_guid,
        ),
    )


async def get_agent_runs(
    db: Executor,
    user_guid: str,
    parent_conversation_id: int,
    *,
    limit: int = 50,
) -> list[Row]:
    """Agent runs of one of the user's conversations, newest first."""
    return await db.fetch_all(
        f"""SELECT * FROM agent_runs
            WHERE parent_conversation_id = ?
            AND parent_conversation_id IN ({OWNED_CONVERSATIONS})
            ORDER BY created_at DESC LIMIT ?""",
        (parent_conversation_id, user_guid, limit),
    )


async def get_agent_run(db: Executor, user_guid: str, agent_id: str) -> Row | None:
    """One of the user's agent runs by agent ID, or None."""
    return await db.fetch_one(
        f"""SELECT * FROM agent_runs WHERE agent_id = ?
            AND parent_conversation_id IN ({OWNED_CONVERSATIONS})""",
        (agent_id, user_guid),
    )
