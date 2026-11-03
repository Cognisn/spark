"""Knowledge graph build jobs: the queue between the knowledge endpoint and the worker.

In server mode the knowledge endpoint does not build in the web process: it
enqueues a job (revision ``kg_build_jobs``) and any web replica reads the job's
status and progress back. The active worker claims queued jobs and builds each
as its owner (:mod:`spark.knowledge.build_jobs`). Personal mode builds
in-process and never uses this table.

A job moves ``queued`` to ``running`` (claimed), then to ``complete``,
``failed`` or ``cancelled``. A failed job goes back to ``queued`` when its owner
asks for a retry. One job per user and scope is active (queued or running) at a
time, enforced by a partial unique index.

Two kinds of function:

* The owner's (:func:`enqueue`, :func:`get`, :func:`active`, :func:`latest`,
  :func:`latest_finished`, :func:`request_cancel`, :func:`retry`) take the
  owner's guid after the executor and never read or change another user's job.
* The worker's (:func:`claim_next`, :func:`record_progress`,
  :func:`is_cancel_requested`, :func:`finish`, :func:`fail_stale`) are
  deliberately unscoped: the worker serves every user, and each job names its
  owner. They are called only by the worker, never from a request.

Claiming takes the oldest queued row with ``FOR UPDATE SKIP LOCKED`` on
PostgreSQL, so two workers claiming at once never take the same job; SQLite
has one writer at a time, and the claim is one guarded ``UPDATE``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from spark.db.engine import Executor, Row
from spark.mode import Dialect

__all__ = [
    "ACTIVE",
    "CANCELLED",
    "INSTANCE_SCOPE",
    "STALE_ERROR",
    "COMPLETE",
    "FAILED",
    "OUTCOMES",
    "QUEUED",
    "RUNNING",
    "BuildJob",
    "RetryOutcome",
    "active",
    "claim_next",
    "enqueue",
    "fail_stale",
    "finish",
    "get",
    "is_cancel_requested",
    "latest",
    "latest_finished",
    "needs_instance_permission",
    "record_progress",
    "request_cancel",
    "retry",
    "should_stop",
]

QUEUED: Final = "queued"
RUNNING: Final = "running"
COMPLETE: Final = "complete"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"

ACTIVE: Final = (QUEUED, RUNNING)
OUTCOMES: Final = (COMPLETE, FAILED, CANCELLED)

STALE_ERROR: Final = "The build stopped before it finished (its worker stopped); retry it"

# The scope whose builds need ``spark.knowledge.instance`` on a server: the graph
# across all of a user's conversations and memories.
INSTANCE_SCOPE: Final = "global"

RetryOutcome = Literal["queued", "not_found", "not_failed", "busy"]
CancelOutcome = Literal["cancelled", "cancelling", "not_building"]

_TABLE: Final = "kg_build_jobs"
_COLUMNS: Final = (
    "id, user_guid, scope, model_id, status, attempts, cancel_requested, claimed_by, "
    "progress, result, error, created_at, updated_at, started_at, finished_at"
)
_QUALIFIED: Final = ", ".join(f"job.{c.strip()}" for c in _COLUMNS.split(","))
_ACTIVE_SQL: Final = "status IN ('queued', 'running')"


@dataclass(frozen=True, slots=True)
class BuildJob:
    """One build job as stored."""

    id: int
    user_guid: str
    scope: str
    model_id: str
    status: str
    attempts: int
    cancel_requested: bool
    claimed_by: str | None
    progress: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    def public(self) -> dict[str, Any]:
        """The job for its owner's page: without the owner's guid or the worker's name."""
        data = asdict(self)
        del data["user_guid"], data["claimed_by"]
        return data


def needs_instance_permission(scope: str) -> bool:
    """Whether building or clearing ``scope`` on a server needs ``spark.knowledge.instance``."""
    return scope == INSTANCE_SCOPE


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _loaded(text: Any) -> dict[str, Any] | None:
    if text is None or text == "":
        return None
    value = json.loads(str(text))
    return value if isinstance(value, dict) else None


def _job(row: Row | None) -> BuildJob | None:
    if row is None:
        return None
    return BuildJob(
        id=int(row["id"]),
        user_guid=str(row["user_guid"]),
        scope=str(row["scope"]),
        model_id=str(row["model_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        cancel_requested=bool(row["cancel_requested"]),
        claimed_by=None if row["claimed_by"] is None else str(row["claimed_by"]),
        progress=_loaded(row["progress"]),
        result=_loaded(row["result"]),
        error=None if row["error"] is None else str(row["error"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        finished_at=None if row["finished_at"] is None else str(row["finished_at"]),
    )


def _is_unique_violation(exc: BaseException) -> bool:
    """Whether the driver refused a write for the active-job index (either engine)."""
    return type(exc).__name__ in ("IntegrityError", "UniqueViolation")


# -- the owner's ----------------------------------------------------------------


async def enqueue(db: Executor, user_guid: str, scope: str, model_id: str) -> BuildJob | None:
    """Queue a build of ``scope`` for ``user_guid``; None when one is already active."""
    now = _now()
    sql = (
        f"INSERT INTO {_TABLE} (user_guid, scope, model_id, status, created_at, updated_at) "
        f"VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_guid, scope) WHERE {_ACTIVE_SQL} "
        "DO NOTHING RETURNING id"
    )
    job_id = await db.fetch_value(sql, (user_guid, scope, model_id, QUEUED, now, now))
    if job_id is None:
        return None
    return await get(db, user_guid, int(job_id))


async def get(db: Executor, user_guid: str, job_id: int) -> BuildJob | None:
    """One of the user's jobs (None for another user's or a missing id)."""
    return _job(
        await db.fetch_one(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ? AND user_guid = ?",
            (job_id, user_guid),
        )
    )


async def active(db: Executor, user_guid: str, scope: str) -> BuildJob | None:
    """The user's queued or running job for ``scope``, if any."""
    return _job(
        await db.fetch_one(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE user_guid = ? AND scope = ? AND {_ACTIVE_SQL}",
            (user_guid, scope),
        )
    )


async def latest(db: Executor, user_guid: str, scope: str) -> BuildJob | None:
    """The user's most recent job for ``scope``, whatever its status."""
    return _job(
        await db.fetch_one(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE user_guid = ? AND scope = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_guid, scope),
        )
    )


async def latest_finished(db: Executor, user_guid: str, scope: str) -> BuildJob | None:
    """The user's most recent finished (complete, failed or cancelled) job for ``scope``."""
    return _job(
        await db.fetch_one(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE user_guid = ? AND scope = ? "
            f"AND NOT {_ACTIVE_SQL} ORDER BY id DESC LIMIT 1",
            (user_guid, scope),
        )
    )


async def request_cancel(db: Executor, user_guid: str, scope: str) -> CancelOutcome:
    """Cancel the user's active job for ``scope``.

    A queued job is cancelled at once; a running one is asked to stop, and its
    worker stops at the next chunk.
    """
    now = _now()
    async with db.transaction() as conn:
        cancelled = await conn.execute(
            f"UPDATE {_TABLE} SET status = ?, finished_at = ?, updated_at = ? "
            "WHERE user_guid = ? AND scope = ? AND status = ?",
            (CANCELLED, now, now, user_guid, scope, QUEUED),
        )
        if cancelled:
            return "cancelled"
        asked = await conn.execute(
            f"UPDATE {_TABLE} SET cancel_requested = 1, updated_at = ? "
            "WHERE user_guid = ? AND scope = ? AND status = ?",
            (now, user_guid, scope, RUNNING),
        )
    return "cancelling" if asked else "not_building"


async def retry(db: Executor, user_guid: str, job_id: int) -> tuple[RetryOutcome, BuildJob | None]:
    """Queue one of the user's failed jobs again.

    ``not_found`` for another user's or a missing job, ``not_failed`` for a job
    that did not fail, ``busy`` while another job of the same scope is active.
    """
    found = await get(db, user_guid, job_id)
    if found is None:
        return "not_found", None
    if found.status != FAILED:
        return "not_failed", found
    try:
        changed = await db.execute(
            f"UPDATE {_TABLE} SET status = ?, cancel_requested = 0, claimed_by = NULL, "
            "progress = NULL, result = NULL, error = NULL, started_at = NULL, "
            "finished_at = NULL, updated_at = ? "
            f"WHERE id = ? AND user_guid = ? AND status = ? AND NOT EXISTS (SELECT 1 FROM {_TABLE} "
            f"other WHERE other.user_guid = ? AND other.scope = ? AND other.{_ACTIVE_SQL})",
            (QUEUED, _now(), job_id, user_guid, FAILED, user_guid, found.scope),
        )
    except Exception as exc:
        if not _is_unique_violation(exc):
            raise
        changed = 0
    if not changed:
        again = await get(db, user_guid, job_id)
        if again is not None and again.status != FAILED:
            return "not_failed", again
        return "busy", again
    return "queued", await get(db, user_guid, job_id)


# -- the worker's (unscoped: never reachable from a request) --------------------


async def claim_next(db: Executor, worker_id: str) -> BuildJob | None:
    """Claim the oldest queued job of any user for ``worker_id``; None when none is queued.

    Deliberately unscoped: the worker builds every user's jobs, each as the
    owner the job names.
    """
    now = _now()
    params: tuple[Any, ...] = (RUNNING, worker_id, now, now)
    if db.dialect is Dialect.POSTGRESQL:
        # The row lock is the claim: a second worker skips the row the first holds.
        sql = (
            f"WITH next AS (SELECT id FROM {_TABLE} WHERE status = 'queued' "
            "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
            f"UPDATE {_TABLE} AS job SET status = ?, claimed_by = ?, attempts = job.attempts + 1, "
            "cancel_requested = 0, started_at = ?, updated_at = ? "
            f"FROM next WHERE job.id = next.id RETURNING {_QUALIFIED}"
        )
    else:
        # One writer at a time (BEGIN IMMEDIATE); the status guard keeps the claim single.
        sql = (
            f"UPDATE {_TABLE} SET status = ?, claimed_by = ?, attempts = attempts + 1, "
            "cancel_requested = 0, started_at = ?, updated_at = ? "
            f"WHERE id = (SELECT id FROM {_TABLE} WHERE status = 'queued' ORDER BY id LIMIT 1) "
            f"AND status = ? RETURNING {_COLUMNS}"
        )
        params = (*params, QUEUED)
    async with db.transaction() as conn:
        return _job(await conn.fetch_one(sql, params))


async def record_progress(
    db: Executor, job_id: int, worker_id: str, progress: dict[str, Any]
) -> bool:
    """Store the running job's progress (and so its heartbeat); False once it is not ours."""
    changed = await db.execute(
        f"UPDATE {_TABLE} SET progress = ?, updated_at = ? "
        "WHERE id = ? AND claimed_by = ? AND status = ?",
        (_json(progress), _now(), job_id, worker_id, RUNNING),
    )
    return bool(changed)


async def is_cancel_requested(db: Executor, job_id: int) -> bool:
    """Whether the job's owner asked a running job to stop."""
    value = await db.fetch_value(f"SELECT cancel_requested FROM {_TABLE} WHERE id = ?", (job_id,))
    return bool(value)


async def should_stop(db: Executor, job_id: int, worker_id: str) -> bool:
    """Whether a build must stop: its owner cancelled it, or it is no longer this worker's.

    A job failed as stale (or finished elsewhere) while its build still runs is
    no longer ``running`` for ``worker_id``; stopping keeps one build per job.
    """
    row = await db.fetch_one(
        f"SELECT status, claimed_by, cancel_requested FROM {_TABLE} WHERE id = ?", (job_id,)
    )
    if row is None:
        return True
    return (
        bool(row["cancel_requested"]) or row["status"] != RUNNING or row["claimed_by"] != worker_id
    )


async def finish(
    db: Executor,
    job_id: int,
    worker_id: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> bool:
    """Record the outcome of a running job this worker claimed; False when it is not ours."""
    if status not in OUTCOMES:
        raise ValueError(f"{status!r} is not a build outcome")
    now = _now()
    changed = await db.execute(
        f"UPDATE {_TABLE} SET status = ?, result = ?, error = ?, finished_at = ?, updated_at = ? "
        "WHERE id = ? AND claimed_by = ? AND status = ?",
        (status, _json(result), error, now, now, job_id, worker_id, RUNNING),
    )
    return bool(changed)


async def fail_stale(db: Executor, *, older_than_seconds: float) -> int:
    """Fail every running job with no progress for ``older_than_seconds``; the count.

    A worker that stopped mid-build (a crash, a lost lock) leaves its job
    running; failing it lets the owner retry. Unscoped, for the worker only.
    """
    cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
    now = _now()
    return await db.execute(
        f"UPDATE {_TABLE} SET status = ?, error = ?, finished_at = ?, updated_at = ? "
        "WHERE status = ? AND updated_at < ?",
        (FAILED, STALE_ERROR, now, now, RUNNING, cutoff),
    )
