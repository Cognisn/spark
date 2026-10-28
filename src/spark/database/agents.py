"""Agent run tracking."""

from __future__ import annotations

from datetime import datetime, timezone

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
    """Record a new agent run. Returns the row ID."""
    new_id = await db.fetch_value(
        """INSERT INTO agent_runs
            (agent_id, parent_conversation_id, agent_name, task_description,
             mode, model_id, user_guid)
            VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            agent_id,
            parent_conversation_id,
            agent_name,
            task_description,
            mode,
            model_id,
            user_guid,
        ),
    )
    return int(new_id)


async def complete_agent_run(
    db: Executor,
    agent_id: str,
    *,
    status: str = "completed",
    result_text: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls_json: str | None = None,
) -> None:
    """Mark an agent run as complete."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE agent_runs SET status = ?, result_text = ?,
            input_tokens = ?, output_tokens = ?,
            tool_calls_json = ?, completed_at = ?
            WHERE agent_id = ?""",
        (status, result_text, input_tokens, output_tokens, tool_calls_json, now, agent_id),
    )


async def get_agent_runs(
    db: Executor,
    parent_conversation_id: int,
    *,
    limit: int = 50,
) -> list[Row]:
    """Get agent runs for a conversation, newest first."""
    return await db.fetch_all(
        """SELECT * FROM agent_runs
            WHERE parent_conversation_id = ?
            ORDER BY created_at DESC LIMIT ?""",
        (parent_conversation_id, limit),
    )


async def get_agent_run(db: Executor, agent_id: str) -> Row | None:
    """Get a single agent run by ID."""
    return await db.fetch_one("SELECT * FROM agent_runs WHERE agent_id = ?", (agent_id,))
