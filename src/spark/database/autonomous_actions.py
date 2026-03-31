"""Autonomous action scheduling and execution tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def create_action(
    db: DatabaseConnection,
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
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        f"""INSERT INTO autonomous_actions
            (name, description, action_prompt, model_id, schedule_type,
             schedule_config, context_mode, max_failures, max_tokens,
             created_at, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (
            name, description, action_prompt, model_id, schedule_type,
            schedule_config, context_mode, max_failures, max_tokens,
            now, user_guid,
        ),
    )
    db.commit()
    return cursor.lastrowid


def get_action(db: DatabaseConnection, action_id: int, user_guid: str) -> dict | None:
    """Get an action by ID."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM autonomous_actions WHERE id = {ph} AND user_guid = {ph}",
        (action_id, user_guid),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def get_enabled_actions(db: DatabaseConnection, user_guid: str) -> list[dict]:
    """Get all enabled actions for a user."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM autonomous_actions WHERE is_enabled = 1 AND user_guid = {ph} ORDER BY name",
        (user_guid,),
    )
    return [dict(row) for row in cursor.fetchall()]


def update_action(
    db: DatabaseConnection, action_id: int, user_guid: str, **kwargs: Any
) -> None:
    """Update action fields."""
    if not kwargs:
        return
    ph = db.placeholder
    sets = ", ".join(f"{k} = {ph}" for k in kwargs)
    params = list(kwargs.values()) + [action_id, user_guid]
    db.execute(
        f"UPDATE autonomous_actions SET {sets}, updated_at = CURRENT_TIMESTAMP, version = version + 1 "
        f"WHERE id = {ph} AND user_guid = {ph}",
        tuple(params),
    )
    db.commit()


def delete_action(db: DatabaseConnection, action_id: int, user_guid: str) -> None:
    """Delete an action and its runs."""
    ph = db.placeholder
    db.execute(f"DELETE FROM action_runs WHERE action_id = {ph}", (action_id,))
    db.execute(
        f"DELETE FROM action_tool_permissions WHERE action_id = {ph}", (action_id,)
    )
    db.execute(
        f"DELETE FROM autonomous_actions WHERE id = {ph} AND user_guid = {ph}",
        (action_id, user_guid),
    )
    db.commit()


def start_run(
    db: DatabaseConnection, action_id: int, user_guid: str
) -> int:
    """Record the start of an action run. Returns run ID."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        f"""INSERT INTO action_runs (action_id, started_at, status, user_guid)
            VALUES ({ph}, {ph}, 'running', {ph})""",
        (action_id, now, user_guid),
    )
    db.execute(
        f"UPDATE autonomous_actions SET last_run_at = {ph} WHERE id = {ph}",
        (now, action_id),
    )
    db.commit()
    return cursor.lastrowid


def complete_run(
    db: DatabaseConnection,
    run_id: int,
    *,
    status: str = "completed",
    result_text: str | None = None,
    error_message: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record the completion of an action run."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        f"""UPDATE action_runs SET
            completed_at = {ph}, status = {ph}, result_text = {ph},
            error_message = {ph}, input_tokens = {ph}, output_tokens = {ph}
            WHERE id = {ph}""",
        (now, status, result_text, error_message, input_tokens, output_tokens, run_id),
    )
    db.commit()


def get_action_runs(
    db: DatabaseConnection, action_id: int, *, limit: int = 20
) -> list[dict]:
    """Get recent runs for an action."""
    ph = db.placeholder
    cursor = db.execute(
        f"SELECT * FROM action_runs WHERE action_id = {ph} ORDER BY started_at DESC LIMIT {ph}",
        (action_id, limit),
    )
    return [dict(row) for row in cursor.fetchall()]


def try_lock_action(db: DatabaseConnection, action_id: int, daemon_id: str) -> bool:
    """Try to acquire an execution lock. Returns True if successful."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        f"""UPDATE autonomous_actions SET locked_by = {ph}, locked_at = {ph}
            WHERE id = {ph} AND (locked_by IS NULL OR locked_by = {ph})""",
        (daemon_id, now, action_id, daemon_id),
    )
    db.commit()
    return cursor.rowcount > 0


def unlock_action(db: DatabaseConnection, action_id: int) -> None:
    """Release the execution lock."""
    ph = db.placeholder
    db.execute(
        f"UPDATE autonomous_actions SET locked_by = NULL, locked_at = NULL WHERE id = {ph}",
        (action_id,),
    )
    db.commit()
