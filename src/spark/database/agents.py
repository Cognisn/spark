"""Agent run tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def create_agent_run(
    db: DatabaseConnection,
    agent_id: str,
    parent_conversation_id: int,
    agent_name: str,
    task_description: str,
    mode: str,
    model_id: str,
    user_guid: str,
) -> int:
    """Record a new agent run. Returns the row ID."""
    ph = db.placeholder
    cursor = db.execute(
        f"""INSERT INTO agent_runs
            (agent_id, parent_conversation_id, agent_name, task_description,
             mode, model_id, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
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
    db.commit()
    return cursor.lastrowid


def complete_agent_run(
    db: DatabaseConnection,
    agent_id: str,
    *,
    status: str = "completed",
    result_text: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls_json: str | None = None,
) -> None:
    """Mark an agent run as complete."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        f"""UPDATE agent_runs SET status = {ph}, result_text = {ph},
            input_tokens = {ph}, output_tokens = {ph},
            tool_calls_json = {ph}, completed_at = {ph}
            WHERE agent_id = {ph}""",
        (status, result_text, input_tokens, output_tokens, tool_calls_json, now, agent_id),
    )
    db.commit()


def get_agent_runs(
    db: DatabaseConnection,
    parent_conversation_id: int,
    *,
    limit: int = 50,
) -> list[dict]:
    """Get agent runs for a conversation, newest first."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT * FROM agent_runs
            WHERE parent_conversation_id = {ph}
            ORDER BY created_at DESC LIMIT {ph}""",
        (parent_conversation_id, limit),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_agent_run(db: DatabaseConnection, agent_id: str) -> dict | None:
    """Get a single agent run by ID."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM agent_runs WHERE agent_id = {ph}",
        (agent_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None
