"""Monthly token budgets: which one applies, how far through it a user is, and the stop (A14).

The limit for a user in a month is decided in this order:

1. the user's own override (:func:`spark.database.usage.user_override`), a
   number or an explicit "unlimited", whatever their roles;
2. on a server, the most generous budget among the user's roles that set one;
3. otherwise none: the user is unlimited.

Personal mode has no roles to budget: its single local user is limited only
once their own budget is set, and usage is recorded either way.

A month is a UTC calendar month (:func:`spark.database.usage.period_of`), so
every budget resets at midnight UTC on the first. At 80% the status is a
warning the UI shows; at 100% :func:`check` refuses a new request with
:class:`BudgetExceeded`. The check is made when a turn, debate step, panel step
or action starts, so a turn already running always finishes, even when it (or
turns started beside it) takes the user past the budget.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from spark.database import usage
from spark.db.bridge import SyncDatabase
from spark.db.engine import Executor

__all__ = ["BudgetExceeded", "BudgetState", "BudgetStatus", "check", "resolve_limit", "status_for"]

BudgetState = Literal["unlimited", "ok", "warning", "exceeded"]

# Month names, kept here so a message never depends on the process's locale.
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# The share of the budget at which the UI warns: 80%, as a fraction kept exact.
_WARNING_NUMERATOR = 4
_WARNING_DENOMINATOR = 5


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        return False


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """A user's usage in one period against the limit that applies to them (None: unlimited)."""

    period: str
    used: int
    limit: int | None

    @property
    def state(self) -> BudgetState:
        if self.limit is None:
            return "unlimited"
        if self.used >= self.limit:
            return "exceeded"
        if self.used * _WARNING_DENOMINATOR >= self.limit * _WARNING_NUMERATOR:
            return "warning"
        return "ok"

    @property
    def percent(self) -> float | None:
        if self.limit is None:
            return None
        return round(self.used * 100 / self.limit, 1)

    @property
    def resets_at(self) -> datetime:
        """When this period's usage stops counting: midnight UTC on the next month's first."""
        return usage.next_period_start(self.period)

    def refusal_message(self) -> str:
        """What a refused user is told."""
        start = usage.period_start(self.period)
        resets = self.resets_at
        return (
            f"You have used your monthly token budget ({self.used:,} of {self.limit or 0:,} "
            f"tokens in {_MONTHS[start.month - 1]} {start.year}). New requests are refused until it resets at 00:00 UTC on "
            f"{resets.day} {_MONTHS[resets.month - 1]} {resets.year}."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "used": self.used,
            "limit": self.limit,
            "percent": self.percent,
            "state": self.state,
            "resets_at": self.resets_at.isoformat(),
        }


class BudgetExceeded(Exception):
    """A new request refused because its user has used their monthly token budget."""

    def __init__(self, status: BudgetStatus) -> None:
        super().__init__(status.refusal_message())
        self.status = status


async def resolve_limit(
    db: Executor, user_guid: str, roles: Iterable[str] | None, *, server: bool
) -> int | None:
    """The monthly token limit that applies to ``user_guid`` holding ``roles`` (None: unlimited)."""
    override = await usage.user_override(db, user_guid)
    if override is not None:
        return override.monthly_tokens
    if not server:
        return None
    role_budgets = await usage.role_budgets(db)
    held = [role_budgets[role] for role in set(roles or ()) if role in role_budgets]
    return max(held) if held else None


async def status_for(
    db: Executor,
    user_guid: str,
    roles: Iterable[str] | None,
    *,
    now: datetime | None = None,
) -> BudgetStatus:
    """``user_guid``'s usage this month (``now``'s, in UTC) against the limit that applies."""
    period = usage.period_of(now or datetime.now(timezone.utc))
    limit = await resolve_limit(db, user_guid, roles, server=_server())
    used = await usage.tokens_in_period(db, user_guid, period)
    return BudgetStatus(period, used, limit)


def check(
    db: SyncDatabase,
    user_guid: str,
    roles: Iterable[str] | None,
    *,
    now: datetime | None = None,
) -> BudgetStatus:
    """The status for a new request from synchronous code; raises :class:`BudgetExceeded` at 100%.

    A refusal is audited (server mode) with the usage and the limit, never content.
    """
    status = db.call(status_for, user_guid, roles, now=now)
    if status.state == "exceeded":
        from spark.identity import audit

        audit.submit(
            audit.USAGE_REFUSED,
            "failure",
            user_guid=user_guid,
            resource_type="budget",
            resource_id=status.period,
            details={"used": status.used, "limit": status.limit, "period": status.period},
        )
        raise BudgetExceeded(status)
    return status
