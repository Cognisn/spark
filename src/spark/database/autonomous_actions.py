"""Autonomous action scheduling and execution tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.db.engine import Executor, Row

__all__ = [
    "complete_run",
    "create_action",
    "delete_action",
    "get_action",
    "get_action_runs",
    "get_enabled_actions",
    "start_run",
    "try_lock_action",
    "unlock_action",
    "update_action",
]


async def create_action(
    db: Executor,
    name: str,
    action_prompt: str,
    model_id: str,
    user_guid: str,
    *,
    description: str | None = None,
    schedule_type: str = "one_off",
    schedule_config: str | None = None,
    context_mode: str = "fresh",
    max_failures: int = 3,
    max_tokens: int = 8192,
) -> int:
    """Create an autonomous action. Returns action ID."""
    now = datetime.now(timezone.utc).isoformat()
    new_id = await db.fetch_value(
        """INSERT INTO autonomous_actions
            (name, description, action_prompt, model_id, schedule_type,
             schedule_config, context_mode, max_failures, max_tokens,
             created_at, user_guid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        (
            name,
            description,
            action_prompt,
            model_id,
            schedule_type,
            schedule_config,
            context_mode,
            max_failures,
            max_tokens,
            now,
            user_guid,
        ),
    )
    return int(new_id)


async def get_action(db: Executor, action_id: int, user_guid: str) -> Row | None:
    """Get an action by ID."""
    return await db.fetch_one(
        "SELECT * FROM autonomous_actions WHERE id = ? AND user_guid = ?",
        (action_id, user_guid),
    )


async def get_enabled_actions(db: Executor, user_guid: str) -> list[Row]:
    """Get all enabled actions for a user."""
    return await db.fetch_all(
        "SELECT * FROM autonomous_actions WHERE is_enabled = 1 AND user_guid = ? ORDER BY name",
        (user_guid,),
    )


_UPDATABLE = frozenset(
    {
        "name",
        "description",
        "action_prompt",
        "model_id",
        "schedule_type",
        "schedule_config",
        "context_mode",
        "max_failures",
        "failure_count",
        "is_enabled",
        "max_tokens",
        "last_run_at",
        "next_run_at",
        "locked_by",
        "locked_at",
    }
)


async def update_action(db: Executor, action_id: int, user_guid: str, **kwargs: Any) -> None:
    """Update action fields (column names are checked against the schema)."""
    if not kwargs:
        return
    unknown = set(kwargs) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown action fields: {', '.join(sorted(unknown))}")
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    params = list(kwargs.values()) + [action_id, user_guid]
    await db.execute(
        f"UPDATE autonomous_actions SET {sets}, updated_at = CURRENT_TIMESTAMP, "
        "version = version + 1 WHERE id = ? AND user_guid = ?",
        tuple(params),
    )


async def delete_action(db: Executor, action_id: int, user_guid: str) -> None:
    """Delete an action and its runs, in one transaction."""
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM action_runs WHERE action_id = ?", (action_id,))
        await conn.execute("DELETE FROM action_tool_permissions WHERE action_id = ?", (action_id,))
        await conn.execute(
            "DELETE FROM autonomous_actions WHERE id = ? AND user_guid = ?",
            (action_id, user_guid),
        )


async def start_run(db: Executor, action_id: int, user_guid: str) -> int:
    """Record the start of an action run, in one transaction. Returns run ID."""
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
        run_id = await conn.fetch_value(
            """INSERT INTO action_runs (action_id, started_at, status, user_guid)
                VALUES (?, ?, 'running', ?) RETURNING id""",
            (action_id, now, user_guid),
        )
        await conn.execute(
            "UPDATE autonomous_actions SET last_run_at = ? WHERE id = ?", (now, action_id)
        )
    return int(run_id)


async def complete_run(
    db: Executor,
    run_id: int,
    *,
    status: str = "completed",
    result_text: str | None = None,
    error_message: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record the completion of an action run."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE action_runs SET
            completed_at = ?, status = ?, result_text = ?,
            error_message = ?, input_tokens = ?, output_tokens = ?
            WHERE id = ?""",
        (now, status, result_text, error_message, input_tokens, output_tokens, run_id),
    )


async def get_action_runs(db: Executor, action_id: int, *, limit: int = 20) -> list[Row]:
    """Get recent runs for an action."""
    return await db.fetch_all(
        "SELECT * FROM action_runs WHERE action_id = ? ORDER BY started_at DESC LIMIT ?",
        (action_id, limit),
    )


async def try_lock_action(db: Executor, action_id: int, daemon_id: str) -> bool:
    """Try to acquire an execution lock. Returns True if successful (one statement, atomic)."""
    now = datetime.now(timezone.utc).isoformat()
    changed = await db.execute(
        """UPDATE autonomous_actions SET locked_by = ?, locked_at = ?
            WHERE id = ? AND (locked_by IS NULL OR locked_by = ?)""",
        (daemon_id, now, action_id, daemon_id),
    )
    return changed > 0


async def unlock_action(db: Executor, action_id: int) -> None:
    """Release the execution lock."""
    await db.execute(
        "UPDATE autonomous_actions SET locked_by = NULL, locked_at = NULL WHERE id = ?",
        (action_id,),
    )
