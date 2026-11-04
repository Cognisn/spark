"""Token usage per model call, and monthly token budgets (A14).

Every model call is one ``usage_tracking`` row: the user it is billed to, the
model, the input and output tokens, where the call came from (:data:`SOURCES`)
and its period, the UTC calendar month it was recorded in. A budget month is a
period, so the monthly reset happens at midnight UTC on the first of the month.
Rows are only ever inserted, so calls recorded at the same time for one user
never lose each other's tokens.

Budgets are monthly token limits: one per role (``usage_role_budgets``) and an
optional override per user (``usage_user_budgets``, where a NULL budget is an
explicit "unlimited"). :mod:`spark.usage.budgets` decides which applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from spark.db.dialect import upsert
from spark.db.engine import Executor, Row

__all__ = [
    "SOURCES",
    "UserOverride",
    "cleanup_old_usage",
    "clear_role_budget",
    "clear_user_override",
    "get_token_usage_in_window",
    "get_usage_summary",
    "next_period_start",
    "period_of",
    "period_start",
    "periods_all_users",
    "record_usage",
    "report_all_users",
    "role_budgets",
    "set_role_budget",
    "set_user_override",
    "tokens_in_period",
    "user_override",
    "user_overrides_all_users",
    "warden_users_all_users",
]

# Where a model call came from.
SOURCES: Final = frozenset({"chat", "compaction", "agent", "debate", "panel", "action"})

_ROLE_BUDGET = upsert(
    "usage_role_budgets",
    ["role", "monthly_tokens"],
    ["role"],
    extra_set=["updated_at = CURRENT_TIMESTAMP"],
)
_USER_OVERRIDE = upsert(
    "usage_user_budgets",
    ["user_guid", "monthly_tokens"],
    ["user_guid"],
    extra_set=["updated_at = CURRENT_TIMESTAMP"],
)


def _utc(at: datetime) -> datetime:
    """``at`` in UTC; a naive time is taken as UTC already."""
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def period_of(at: datetime) -> str:
    """The budget period (``YYYY-MM``, the UTC calendar month) that ``at`` falls in."""
    return _utc(at).strftime("%Y-%m")


def period_start(period: str) -> datetime:
    """Midnight UTC on the first day of ``period``."""
    return datetime.strptime(period + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)


def next_period_start(period: str) -> datetime:
    """Midnight UTC on the first day of the month after ``period``: when its budget resets."""
    start = period_start(period)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _positive(monthly_tokens: int) -> int:
    if isinstance(monthly_tokens, bool) or not isinstance(monthly_tokens, int):
        raise ValueError("a budget is a whole number of tokens")
    if monthly_tokens < 1:
        raise ValueError("a budget must be a positive number of tokens")
    return monthly_tokens


async def record_usage(
    db: Executor,
    conversation_id: int | None,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    user_guid: str,
    *,
    region: str | None = None,
    cost: float = 0.0,
    source: str = "chat",
    action_id: int | None = None,
    at: datetime | None = None,
) -> None:
    """Record one model call billed to ``user_guid`` (now, unless ``at`` is given)."""
    if source not in SOURCES:
        raise ValueError(f"unknown usage source {source!r}")
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    when = _utc(at or datetime.now(timezone.utc))
    await db.execute(
        """INSERT INTO usage_tracking
            (conversation_id, model_id, region, input_tokens, output_tokens, cost,
             timestamp, user_guid, source, action_id, period)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            model_id,
            region,
            int(input_tokens),
            int(output_tokens),
            cost,
            when.strftime("%Y-%m-%d %H:%M:%S.%f"),
            user_guid,
            source,
            action_id,
            period_of(when),
        ),
    )


async def tokens_in_period(db: Executor, user_guid: str, period: str) -> int:
    """Input plus output tokens billed to ``user_guid`` in ``period``."""
    total = await db.fetch_value(
        """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM usage_tracking
            WHERE user_guid = ? AND period = ?""",
        (user_guid, period),
    )
    return int(total) if total is not None else 0


async def report_all_users(db: Executor, *, period: str | None = None) -> list[Row]:
    """Usage by period, user and model, newest period first (one period when given).

    Deliberately not scoped by user: the administrator's usage view and its
    export, reachable only behind ``spark.usage.admin``
    (:mod:`spark.web.endpoints.usage`).
    """
    where = "WHERE period = ?" if period is not None else ""
    params: tuple[Any, ...] = (period,) if period is not None else ()
    rows = await db.fetch_all(
        f"""SELECT period, user_guid, model_id,
                   COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM usage_tracking {where}
            GROUP BY period, user_guid, model_id
            ORDER BY period DESC, user_guid, model_id""",
        params,
    )
    for row in rows:
        row["requests"] = int(row["requests"])
        row["input_tokens"] = int(row["input_tokens"])
        row["output_tokens"] = int(row["output_tokens"])
    return rows


async def periods_all_users(db: Executor) -> list[str]:
    """Every period with usage, newest first.

    Deliberately not scoped by user: the administrator's usage view only.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT period FROM usage_tracking WHERE period <> '' ORDER BY period DESC"
    )
    return [str(r["period"]) for r in rows]


async def role_budgets(db: Executor) -> dict[str, int]:
    """Each role's monthly token budget (roles without one are absent)."""
    rows = await db.fetch_all("SELECT role, monthly_tokens FROM usage_role_budgets")
    return {str(r["role"]): int(r["monthly_tokens"]) for r in rows}


async def set_role_budget(db: Executor, role: str, monthly_tokens: int) -> None:
    """Set (or replace) ``role``'s monthly token budget."""
    await db.execute(_ROLE_BUDGET, (role, _positive(monthly_tokens)))


async def clear_role_budget(db: Executor, role: str) -> None:
    """Remove ``role``'s budget: the role no longer limits anyone."""
    await db.execute("DELETE FROM usage_role_budgets WHERE role = ?", (role,))


@dataclass(frozen=True, slots=True)
class UserOverride:
    """A user's own budget, in place of their roles'. None means explicitly unlimited."""

    monthly_tokens: int | None


async def user_override(db: Executor, user_guid: str) -> UserOverride | None:
    """``user_guid``'s override, or None when their roles decide."""
    row = await db.fetch_one(
        "SELECT monthly_tokens FROM usage_user_budgets WHERE user_guid = ?", (user_guid,)
    )
    if row is None:
        return None
    tokens = row["monthly_tokens"]
    return UserOverride(None if tokens is None else int(tokens))


async def set_user_override(db: Executor, user_guid: str, monthly_tokens: int | None) -> None:
    """Give ``user_guid`` their own monthly budget (None: unlimited, whatever their roles)."""
    tokens = None if monthly_tokens is None else _positive(monthly_tokens)
    await db.execute(_USER_OVERRIDE, (user_guid, tokens))


async def clear_user_override(db: Executor, user_guid: str) -> None:
    """Remove ``user_guid``'s override: their roles decide again."""
    await db.execute("DELETE FROM usage_user_budgets WHERE user_guid = ?", (user_guid,))


async def user_overrides_all_users(db: Executor) -> dict[str, int | None]:
    """Every user's override (None: unlimited).

    Deliberately not scoped by user: the administrator's budget settings only.
    """
    rows = await db.fetch_all("SELECT user_guid, monthly_tokens FROM usage_user_budgets")
    return {
        str(r["user_guid"]): None if r["monthly_tokens"] is None else int(r["monthly_tokens"])
        for r in rows
    }


async def warden_users_all_users(db: Executor) -> dict[str, str]:
    """Every SPARK user guid linked to a warden user, with that user's id.

    Deliberately not scoped by user: the administrator's usage view names each
    user by email address. Read only: unlike
    :func:`spark.database.user_identities.user_guid_for`, it never creates a guid.
    """
    rows = await db.fetch_all("SELECT user_guid, warden_user_id FROM user_identities")
    return {str(r["user_guid"]): str(r["warden_user_id"]) for r in rows}


async def get_token_usage_in_window(db: Executor, user_guid: str, since: str) -> dict[str, Any]:
    """Get total input/output tokens since a timestamp."""
    row = await db.fetch_one(
        """SELECT COALESCE(SUM(input_tokens), 0) as input_tokens,
                  COALESCE(SUM(output_tokens), 0) as output_tokens,
                  COALESCE(SUM(cost), 0.0) as total_cost
           FROM usage_tracking WHERE user_guid = ? AND timestamp >= ?""",
        (user_guid, since),
    )
    if row is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}
    return {
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "total_cost": float(row["total_cost"]),
    }


async def get_usage_summary(db: Executor, user_guid: str, since: str) -> list[Row]:
    """Get usage breakdown by model and region."""
    rows = await db.fetch_all(
        """SELECT model_id, region,
                  SUM(input_tokens) as input_tokens,
                  SUM(output_tokens) as output_tokens,
                  SUM(cost) as total_cost,
                  COUNT(*) as request_count
           FROM usage_tracking WHERE user_guid = ? AND timestamp >= ?
           GROUP BY model_id, region
           ORDER BY total_cost DESC""",
        (user_guid, since),
    )
    for row in rows:
        row["input_tokens"] = int(row["input_tokens"])
        row["output_tokens"] = int(row["output_tokens"])
        row["total_cost"] = float(row["total_cost"])
        row["request_count"] = int(row["request_count"])
    return rows


async def cleanup_old_usage(db: Executor, before: str) -> int:
    """Delete usage records before a cutoff timestamp. Returns count deleted.

    Deliberately not scoped by user: retention maintenance over every user's
    records, for an operator or the worker. Never reached from a user's request.
    """
    return await db.execute("DELETE FROM usage_tracking WHERE timestamp < ?", (before,))
