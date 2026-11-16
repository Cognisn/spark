"""Usage per model call, with its credits (A14, M6 to M10).

Every model call is one ``usage_tracking`` row: the user it is billed to, the
connection and model it ran on, its four token counts apart (uncached input,
output, cache read and cache write), where the call came from (:data:`SOURCES`,
the task), its period (the UTC calendar month it was recorded in) and its credits.

A call's credits are worked out once, when it is recorded, from its connection's
rates (``connection_metering``) and its model's tier (``connection_models``) by
:func:`spark.usage.credits.credits`, and stored in whole micro-credits
(``credits_micro``) beside the rates used (``credit_inputs``, JSON). A later change
of a connection's rates never rewrites what earlier calls were charged. Rows are
only ever inserted, so calls recorded at the same time for one user never lose each
other's credits or tokens.

A month is a period, so allowances reset at midnight UTC on the first of the month.
Allowances themselves live on the connection assignments
(:mod:`spark.database.connections`); :mod:`spark.usage.budgets` decides which applies.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Final

from spark.db.engine import Executor, Row
from spark.usage import credits as credit_calc

__all__ = [
    "SOURCES",
    "AllowanceRow",
    "allowance_rows",
    "cleanup_old_usage",
    "connection_has_assignments",
    "connection_totals",
    "credits_in_period",
    "get_token_usage_in_window",
    "get_usage_summary",
    "next_period_start",
    "period_of",
    "period_start",
    "periods_all_users",
    "rates_for",
    "record_usage",
    "report_all_users",
    "tokens_in_period",
    "warden_users_all_users",
]

# Where a model call came from.
SOURCES: Final = frozenset(
    {"chat", "compaction", "agent", "debate", "panel", "action", "knowledge"}
)


def _utc(at: datetime) -> datetime:
    """``at`` in UTC; a naive time is taken as UTC already."""
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def period_of(at: datetime) -> str:
    """The period (``YYYY-MM``, the UTC calendar month) that ``at`` falls in."""
    return _utc(at).strftime("%Y-%m")


def period_start(period: str) -> datetime:
    """Midnight UTC on the first day of ``period``."""
    return datetime.strptime(period + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)


def next_period_start(period: str) -> datetime:
    """Midnight UTC on the first day of the month after ``period``: when allowances reset."""
    start = period_start(period)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


# -- Rates ----------------------------------------------------------------------------


async def rates_for(
    db: Executor, connection_id: str, model_id: str
) -> tuple[credit_calc.Rates, str, bool]:
    """The rates, the model's tier and whether the connection is metered, as they are now.

    A connection with no metering saved is unmetered at the default rates; a model
    with no stored tier is standard.
    """
    from spark.database.connections import get_metering

    metering = await get_metering(db, connection_id)
    tier = await db.fetch_value(
        "SELECT tier FROM connection_models WHERE connection_id = ? AND model_id = ?",
        (connection_id, model_id),
    )
    tier_name = str(tier) if tier in credit_calc.TIERS else "standard"
    return credit_calc.Rates.from_metering(metering), tier_name, metering.metered


# -- Recording ------------------------------------------------------------------------


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
    connection_id: str | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Record one model call billed to ``user_guid`` (now, unless ``at`` is given).

    ``input_tokens`` is uncached input; cache reads and writes are counted apart. A
    call on a connection is charged its credits at the connection's rates now, and the
    rates are kept with it; a call with no connection costs no credits.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown usage source {source!r}")
    counts = (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    if any(count < 0 for count in counts):
        raise ValueError("token counts cannot be negative")
    micro, inputs = 0, None
    if connection_id:
        rates, tier, metered = await rates_for(db, connection_id, model_id)
        charged = credit_calc.credits(
            rates, credit_calc.TokenCounts(*(int(c) for c in counts)), tier
        )
        micro = credit_calc.to_micro(charged)
        inputs = json.dumps(rates.as_record(tier, metered=metered), sort_keys=True)
    when = _utc(at or datetime.now(timezone.utc))
    await db.execute(
        """INSERT INTO usage_tracking
            (conversation_id, model_id, region, input_tokens, output_tokens, cost,
             timestamp, user_guid, source, action_id, period, connection_id,
             cache_read_tokens, cache_write_tokens, credits_micro, credit_inputs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            connection_id,
            int(cache_read_tokens),
            int(cache_write_tokens),
            micro,
            inputs,
        ),
    )


# Every token a call is billed for: uncached input, output, cache reads and writes.
_ALL_TOKENS = "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens"
# Input as A14 counted it: uncached input with the cache reads and writes.
_ALL_INPUT = "input_tokens + cache_read_tokens + cache_write_tokens"


async def tokens_in_period(db: Executor, user_guid: str, period: str) -> int:
    """Every token (input, output, cache read and cache write) billed to ``user_guid`` in
    ``period``, on every connection."""
    total = await db.fetch_value(
        f"""SELECT COALESCE(SUM({_ALL_TOKENS}), 0) FROM usage_tracking
            WHERE user_guid = ? AND period = ?""",
        (user_guid, period),
    )
    return int(total) if total is not None else 0


async def credits_in_period(
    db: Executor, user_guid: str, connection_id: str, period: str
) -> Decimal:
    """The credits ``user_guid`` used on ``connection_id`` in ``period``."""
    total = await db.fetch_value(
        """SELECT COALESCE(SUM(credits_micro), 0) FROM usage_tracking
            WHERE user_guid = ? AND connection_id = ? AND period = ?""",
        (user_guid, connection_id, period),
    )
    return credit_calc.from_micro(int(total) if total is not None else 0)


# -- Allowances -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllowanceRow:
    """One assignment that may set a user's allowance on a connection.

    ``user_guid`` is set for the user's own assignment, ``role`` for a role's.
    ``monthly_credits`` is None when the assignment sets no limit.
    """

    role: str | None
    user_guid: str | None
    monthly_credits: int | None


async def allowance_rows(
    db: Executor, connection_id: str, user_guid: str, roles: Iterable[str]
) -> list[AllowanceRow]:
    """The assignments of ``connection_id`` to ``user_guid`` or to any of ``roles``."""
    held = sorted(set(roles))
    marks = ", ".join("?" for _ in held)
    role_clause = f" OR role IN ({marks})" if held else ""
    rows = await db.fetch_all(
        "SELECT role, user_guid, monthly_credits FROM connection_assignments "
        f"WHERE connection_id = ? AND (user_guid = ?{role_clause}) ORDER BY id",
        (connection_id, user_guid, *held),
    )
    return [
        AllowanceRow(
            role=None if r["role"] is None else str(r["role"]),
            user_guid=None if r["user_guid"] is None else str(r["user_guid"]),
            monthly_credits=None if r["monthly_credits"] is None else int(r["monthly_credits"]),
        )
        for r in rows
    ]


async def connection_has_assignments(db: Executor, connection_id: str) -> bool:
    """Whether ``connection_id`` is assigned to anyone (it says nothing about whom)."""
    found = await db.fetch_value(
        "SELECT 1 FROM connection_assignments WHERE connection_id = ? LIMIT 1", (connection_id,)
    )
    return found is not None


# -- The administrator's view ---------------------------------------------------------


def _counts(row: Row) -> None:
    for key in (
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        row[key] = int(row[key])
    row["credits"] = credit_calc.from_micro(int(row.pop("credits_micro")))


_SUMS = """COUNT(*) AS requests,
           COALESCE(SUM(input_tokens), 0) AS input_tokens,
           COALESCE(SUM(output_tokens), 0) AS output_tokens,
           COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
           COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
           COALESCE(SUM(credits_micro), 0) AS credits_micro"""


async def report_all_users(db: Executor, *, period: str | None = None) -> list[Row]:
    """Usage by period, user, connection and model, newest period first (one period when
    given): requests, the four token counts and the credits.

    Deliberately not scoped by user: the administrator's usage view and its
    export, reachable only behind ``spark.usage.admin``
    (:mod:`spark.web.endpoints.usage`).
    """
    where = "WHERE period = ?" if period is not None else ""
    params: tuple[Any, ...] = (period,) if period is not None else ()
    rows = await db.fetch_all(
        f"""SELECT period, user_guid, COALESCE(connection_id, '') AS connection_id, model_id,
                   {_SUMS}
            FROM usage_tracking {where}
            GROUP BY period, user_guid, COALESCE(connection_id, ''), model_id
            ORDER BY period DESC, user_guid, connection_id, model_id""",
        params,
    )
    for row in rows:
        _counts(row)
    return rows


async def connection_totals(db: Executor, *, period: str | None = None) -> list[Row]:
    """Usage per connection (one period when given): requests, tokens and credits.

    Deliberately not scoped by user: the administrator's usage view only.
    """
    where = "WHERE period = ?" if period is not None else ""
    params: tuple[Any, ...] = (period,) if period is not None else ()
    rows = await db.fetch_all(
        f"""SELECT COALESCE(connection_id, '') AS connection_id, {_SUMS}
            FROM usage_tracking {where}
            GROUP BY COALESCE(connection_id, '')
            ORDER BY connection_id""",
        params,
    )
    for row in rows:
        _counts(row)
    return rows


async def periods_all_users(db: Executor) -> list[str]:
    """Every period with usage, newest first.

    Deliberately not scoped by user: the administrator's usage view only.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT period FROM usage_tracking WHERE period <> '' ORDER BY period DESC"
    )
    return [str(r["period"]) for r in rows]


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
        f"""SELECT COALESCE(SUM({_ALL_INPUT}), 0) as input_tokens,
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
        f"""SELECT model_id, region,
                  SUM({_ALL_INPUT}) as input_tokens,
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
