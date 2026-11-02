"""Autonomous action scheduling and execution tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.database.scope import OWNED_ACTIONS, NotOwned, owns_action
from spark.db.dialect import upsert
from spark.db.engine import Executor, Row

__all__ = [
    "DuplicateActionName",
    "action_stats",
    "clear_stale_locks",
    "get_all_actions",
    "get_tool_permissions",
    "set_tool_permission",
    "complete_run",
    "create_action",
    "delete_action",
    "get_action",
    "get_action_runs",
    "get_enabled_actions",
    "get_enabled_actions_for_worker",
    "record_refused_run",
    "start_run",
    "try_lock_action",
    "unlock_action",
    "update_action",
]


class DuplicateActionName(ValueError):
    """The user already has an action with this name (names are unique per user)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"You already have an action named {name!r}.")
        self.name = name


async def _check_name_free(
    db: Executor, user_guid: str, name: str, *, except_id: int | None = None
) -> None:
    """Raise :class:`DuplicateActionName` if ``user_guid`` already has an action ``name``.

    Only the user's own actions are looked at: another user's names are free,
    and a clash never says anything about another user.
    """
    clash = await db.fetch_value(
        "SELECT id FROM autonomous_actions WHERE user_guid = ? AND name = ? AND id <> ?",
        (user_guid, name, -1 if except_id is None else except_id),
    )
    if clash is not None:
        raise DuplicateActionName(name)


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
    """Create one of the user's actions. Returns action ID.

    Raises :class:`DuplicateActionName` when the user already has an action of
    that name (another user's names do not count).
    """
    now = datetime.now(timezone.utc).isoformat()
    await _check_name_free(db, user_guid, name)
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


async def get_enabled_actions_for_worker(db: Executor) -> list[Row]:
    """Every user's enabled actions, each row carrying its owner in ``user_guid``.

    Deliberately not scoped by user: only the server's worker calls this, to
    schedule every action and then run each one as its owner (with the owner's
    guid and current permissions). Never reached from a user's request; anything
    serving a user lists with :func:`get_enabled_actions` or :func:`get_all_actions`.
    """
    return await db.fetch_all(
        "SELECT * FROM autonomous_actions WHERE is_enabled = 1 ORDER BY user_guid, name"
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
    """Update action fields (column names are checked against the schema).

    A new name must not be one of the user's other actions' names
    (:class:`DuplicateActionName`).
    """
    if not kwargs:
        return
    unknown = set(kwargs) - _UPDATABLE
    if unknown:
        raise ValueError(f"unknown action fields: {', '.join(sorted(unknown))}")
    if "name" in kwargs:
        await _check_name_free(db, user_guid, str(kwargs["name"]), except_id=action_id)
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    params = list(kwargs.values()) + [action_id, user_guid]
    await db.execute(
        f"UPDATE autonomous_actions SET {sets}, updated_at = CURRENT_TIMESTAMP, "
        "version = version + 1 WHERE id = ? AND user_guid = ?",
        tuple(params),
    )


async def delete_action(db: Executor, action_id: int, user_guid: str) -> None:
    """Delete one of the user's actions and its runs, in one transaction."""
    async with db.transaction() as conn:
        if not await owns_action(conn, user_guid, action_id):
            return
        await conn.execute("DELETE FROM action_runs WHERE action_id = ?", (action_id,))
        await conn.execute("DELETE FROM action_tool_permissions WHERE action_id = ?", (action_id,))
        await conn.execute(
            "DELETE FROM autonomous_actions WHERE id = ? AND user_guid = ?",
            (action_id, user_guid),
        )


async def start_run(db: Executor, action_id: int, user_guid: str) -> int:
    """Record the start of a run of one of the user's actions, in one transaction.

    Returns the run ID. Raises :class:`NotOwned` when the action is not the user's.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
        if not await owns_action(conn, user_guid, action_id):
            raise NotOwned(f"action {action_id} not found")
        run_id = await conn.fetch_value(
            """INSERT INTO action_runs (action_id, started_at, status, user_guid)
                VALUES (?, ?, 'running', ?) RETURNING id""",
            (action_id, now, user_guid),
        )
        await conn.execute(
            "UPDATE autonomous_actions SET last_run_at = ? WHERE id = ? AND user_guid = ?",
            (now, action_id, user_guid),
        )
    return int(run_id)


async def record_refused_run(db: Executor, action_id: int, user_guid: str, reason: str) -> int:
    """Record that a run of one of the user's actions was refused, and why. Returns the run ID.

    The run is complete at once with status ``refused``; the action keeps its
    last run time and stays enabled, so it runs again once its owner may run it.
    Raises :class:`NotOwned` when the action is not the user's.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with db.transaction() as conn:
        if not await owns_action(conn, user_guid, action_id):
            raise NotOwned(f"action {action_id} not found")
        run_id = await conn.fetch_value(
            """INSERT INTO action_runs
                (action_id, started_at, completed_at, status, error_message, user_guid)
                VALUES (?, ?, ?, 'refused', ?, ?) RETURNING id""",
            (action_id, now, now, reason, user_guid),
        )
    return int(run_id)


async def complete_run(
    db: Executor,
    user_guid: str,
    run_id: int,
    *,
    status: str = "completed",
    result_text: str | None = None,
    error_message: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record the completion of a run of one of the user's actions."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        f"""UPDATE action_runs SET
            completed_at = ?, status = ?, result_text = ?,
            error_message = ?, input_tokens = ?, output_tokens = ?
            WHERE id = ? AND action_id IN ({OWNED_ACTIONS})""",
        (now, status, result_text, error_message, input_tokens, output_tokens, run_id, user_guid),
    )


async def get_action_runs(
    db: Executor, user_guid: str, action_id: int, *, limit: int = 20
) -> list[Row]:
    """Recent runs of one of the user's actions (none for another user's action)."""
    return await db.fetch_all(
        f"""SELECT * FROM action_runs WHERE action_id = ? AND action_id IN ({OWNED_ACTIONS})
            ORDER BY started_at DESC LIMIT ?""",
        (action_id, user_guid, limit),
    )


async def try_lock_action(db: Executor, action_id: int, daemon_id: str) -> bool:
    """Try to acquire an execution lock. Returns True if successful (one statement, atomic).

    Deliberately not scoped by user: the lock belongs to the worker running the
    action, which loaded it for its owner first. Never reached from a user's request.
    """
    now = datetime.now(timezone.utc).isoformat()
    changed = await db.execute(
        """UPDATE autonomous_actions SET locked_by = ?, locked_at = ?
            WHERE id = ? AND (locked_by IS NULL OR locked_by = ?)""",
        (daemon_id, now, action_id, daemon_id),
    )
    return changed > 0


async def unlock_action(db: Executor, action_id: int) -> None:
    """Release the execution lock (the worker's; deliberately not scoped by user)."""
    await db.execute(
        "UPDATE autonomous_actions SET locked_by = NULL, locked_at = NULL WHERE id = ?",
        (action_id,),
    )


async def get_all_actions(db: Executor, user_guid: str) -> list[Row]:
    """Every one of the user's actions, enabled or not, by name."""
    return await db.fetch_all(
        "SELECT * FROM autonomous_actions WHERE user_guid = ? ORDER BY name", (user_guid,)
    )


_TOOL_UPSERT = upsert(
    "action_tool_permissions",
    ["action_id", "tool_name", "permission_state", "user_guid"],
    ["action_id", "tool_name"],
)


async def get_tool_permissions(db: Executor, user_guid: str, action_id: int) -> dict[str, str]:
    """Tool permission states for one of the user's actions, keyed by tool name."""
    rows = await db.fetch_all(
        f"""SELECT tool_name, permission_state FROM action_tool_permissions
            WHERE action_id = ? AND action_id IN ({OWNED_ACTIONS})""",
        (action_id, user_guid),
    )
    return {r["tool_name"]: r["permission_state"] for r in rows}


async def set_tool_permission(
    db: Executor, action_id: int, tool_name: str, permission_state: str, user_guid: str
) -> bool:
    """Set or update one tool's permission for one of the user's actions.

    Returns False, changing nothing, when the action is not the user's.
    """
    async with db.transaction() as conn:
        if not await owns_action(conn, user_guid, action_id):
            return False
        await conn.execute(_TOOL_UPSERT, (action_id, tool_name, permission_state, user_guid))
    return True


async def clear_stale_locks(db: Executor, before: str) -> list[Row]:
    """Release locks taken before ``before`` (ISO UTC). Returns the actions released.

    Deliberately not scoped by user: maintenance by the single scheduler worker
    over every action. Never reached from a user's request.
    """
    async with db.transaction() as conn:
        stale = await conn.fetch_all(
            """SELECT id, name, locked_by, locked_at FROM autonomous_actions
                WHERE locked_by IS NOT NULL AND locked_at IS NOT NULL AND locked_at < ?
                ORDER BY id""",
            (before,),
        )
        for row in stale:
            await conn.execute(
                "UPDATE autonomous_actions SET locked_by = NULL, locked_at = NULL WHERE id = ?",
                (row["id"],),
            )
    return stale


async def action_stats(db: Executor, user_guid: str) -> dict[str, Any]:
    """The user's enabled actions, last completed run and next scheduled run (tray menu)."""
    total = await db.fetch_value(
        "SELECT COUNT(*) FROM autonomous_actions WHERE is_enabled = 1 AND user_guid = ?",
        (user_guid,),
    )
    last_run = await db.fetch_value(
        f"SELECT completed_at FROM action_runs WHERE status = 'completed' "
        f"AND action_id IN ({OWNED_ACTIONS}) ORDER BY completed_at DESC LIMIT 1",
        (user_guid,),
    )
    next_run = await db.fetch_value(
        "SELECT next_run_at FROM autonomous_actions WHERE is_enabled = 1 AND user_guid = ? "
        "AND next_run_at IS NOT NULL ORDER BY next_run_at ASC LIMIT 1",
        (user_guid,),
    )
    return {
        "total": int(total or 0),
        "last_run": str(last_run) if last_run else None,
        "next_run": str(next_run) if next_run else None,
    }
