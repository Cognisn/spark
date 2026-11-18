"""Monthly credit allowances per connection: which applies, the standing, and the stop (M6 to M10).

Credits are counted per connection (:mod:`spark.usage.credits`). On a server, a
user's monthly allowance on a **metered** connection is decided in this order:

1. the user's own assignment of the connection, if any (M9): its allowance, or no
   limit when it sets none;
2. otherwise the highest allowance among the assignments of the connection to the
   user's roles (M10); no limit when one of them sets none;
3. otherwise, when the connection is assigned to someone, no allowance at all (0:
   the connection is not theirs, and wave 8's assignment check refuses it anyway);
   a connection assigned to no one sets no limit (only an administrator reaches it).

An unmetered connection never stops anyone, and personal mode meters nothing (M3).

A month is a UTC calendar month (:func:`spark.database.usage.period_of`), so every
allowance resets at midnight UTC on the first. At 80% the status is a warning the
UI shows; at 100% :func:`check` refuses a new request on that connection with
:class:`BudgetExceeded`, whose message names the connection and the reset date.
The check is made when a turn, debate or panel step, scheduled action or knowledge
graph chunk starts, so a turn already running always finishes, even when it (or
turns started beside it) takes the user past the allowance.

:func:`status_for` and :func:`check` answer for the connection a request will use:
``connection_id``, or ``model`` (a ``connection/model`` reference or a bare model
id) resolved through the process's connection runtime. Without either,
:func:`status_for` answers the user's most used metered connection (the standing a
page shows after a turn) and :func:`check` refuses nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from spark.database import usage
from spark.db.bridge import SyncDatabase
from spark.db.engine import Executor

__all__ = [
    "BudgetExceeded",
    "BudgetState",
    "BudgetStatus",
    "check",
    "connection_of",
    "resolve_allowance",
    "standings",
    "status_for",
]

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

# The share of the allowance at which the UI warns: 80%, as a fraction kept exact.
_WARNING_NUMERATOR = 4
_WARNING_DENOMINATOR = 5

_RANK: dict[str, int] = {"unlimited": 0, "ok": 1, "warning": 2, "exceeded": 3}


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        return False


def _amount(value: Decimal) -> str:
    """Credits as people read them: whole numbers bare, others to two places."""
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:,.2f}"


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """A user's credits used on one connection in one period, against their allowance.

    ``limit`` None means no limit (an unmetered connection, personal mode, or an
    assignment that sets none). ``connection_id`` is None only when the user has no
    metered connection to report.
    """

    period: str
    used: Decimal = Decimal(0)
    limit: Decimal | None = None
    connection_id: str | None = None
    connection: str | None = None

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
        if self.limit == 0:
            return 100.0
        return round(float(self.used * 100 / self.limit), 1)

    @property
    def remaining(self) -> Decimal | None:
        """Credits left this period (None: no limit)."""
        if self.limit is None:
            return None
        return max(self.limit - self.used, Decimal(0))

    @property
    def resets_at(self) -> datetime:
        """When this period's credits stop counting: midnight UTC on the next month's first."""
        return usage.next_period_start(self.period)

    @property
    def label(self) -> str:
        return self.connection or self.connection_id or "this connection"

    def refusal_message(self) -> str:
        """What a refused user is told: the connection, the usage and the reset date."""
        start = usage.period_start(self.period)
        resets = self.resets_at
        name = self.label
        return (
            f"You have used your monthly credits on {name} ({_amount(self.used)} of "
            f"{_amount(self.limit or Decimal(0))} credits in {_MONTHS[start.month - 1]} "
            f"{start.year}). New requests on {name} are refused until they reset at 00:00 UTC "
            f"on {resets.day} {_MONTHS[resets.month - 1]} {resets.year}."
        )

    def as_dict(self) -> dict[str, Any]:
        remaining = self.remaining
        return {
            "period": self.period,
            "connection_id": self.connection_id,
            "connection": self.connection,
            "unit": "credits",
            "used": float(self.used),
            "limit": None if self.limit is None else float(self.limit),
            "remaining": None if remaining is None else float(remaining),
            "percent": self.percent,
            "state": self.state,
            "resets_at": self.resets_at.isoformat(),
        }


class BudgetExceeded(Exception):
    """A new request refused because its user has used their allowance on its connection."""

    def __init__(self, status: BudgetStatus) -> None:
        super().__init__(status.refusal_message())
        self.status = status


def connection_of(model: str | None, service: object = None) -> str | None:
    """The connection a request on ``model`` will use (None when it cannot be told).

    ``service`` is the request's own service when there is one: its reference
    decides. Otherwise a ``connection/model`` reference, or a bare model id the
    process's connection runtime resolves.
    """
    from spark.connections.refs import ModelRef

    ref = getattr(service, "ref", None) if service is not None else None
    if isinstance(ref, ModelRef):
        return ref.connection_id
    if not model:
        return None
    from spark.connections import startup

    runtime = startup.current()
    if runtime is not None:
        resolved = runtime.resolve(model)
        if resolved is not None:
            return resolved.connection_id
    parsed = ModelRef.parse(model)
    return parsed.connection_id if parsed is not None else None


async def resolve_allowance(
    db: Executor, user_guid: str, roles: Iterable[str] | None, connection_id: str
) -> Decimal | None:
    """``user_guid``'s monthly credit allowance on ``connection_id`` (None: no limit).

    Only for a metered connection on a server; see the module's order.
    """
    rows = await usage.allowance_rows(db, connection_id, user_guid, roles or ())
    own = [r for r in rows if r.user_guid == user_guid]
    if own:
        allowance = own[0].monthly_credits
        return None if allowance is None else Decimal(allowance)
    held = [r for r in rows if r.role is not None]
    if held:
        if any(r.monthly_credits is None for r in held):
            return None
        return Decimal(max(int(r.monthly_credits or 0) for r in held))
    if not await usage.connection_has_assignments(db, connection_id):
        return None
    return Decimal(0)


async def _connection_name(db: Executor, connection_id: str) -> str:
    from spark.database.connections import get_connection

    row = await get_connection(db, connection_id)
    return row.name if row is not None else connection_id


async def _standing(
    db: Executor, user_guid: str, roles: Iterable[str] | None, connection_id: str, period: str
) -> BudgetStatus:
    from spark.database.connections import get_metering

    metering = await get_metering(db, connection_id)
    used = await usage.credits_in_period(db, user_guid, connection_id, period)
    limit = (
        await resolve_allowance(db, user_guid, roles, connection_id) if metering.metered else None
    )
    return BudgetStatus(
        period,
        used=used,
        limit=limit,
        connection_id=connection_id,
        connection=await _connection_name(db, connection_id),
    )


async def standings(
    db: Executor,
    user_guid: str,
    roles: Iterable[str] | None,
    *,
    now: datetime | None = None,
) -> list[BudgetStatus]:
    """The user's standing this month on each metered connection assigned to them or to
    their roles, by connection id (none in personal mode)."""
    if not _server():
        return []
    from spark.database.connections import connections_for, get_metering

    period = usage.period_of(now or datetime.now(timezone.utc))
    held = frozenset(roles or ())
    found: list[BudgetStatus] = []
    for connection_id in sorted(await connections_for(db, user_guid, held)):
        if (await get_metering(db, connection_id)).metered:
            found.append(await _standing(db, user_guid, held, connection_id, period))
    return found


async def status_for(
    db: Executor,
    user_guid: str,
    roles: Iterable[str] | None,
    *,
    now: datetime | None = None,
    connection_id: str | None = None,
    model: str | None = None,
) -> BudgetStatus:
    """``user_guid``'s credits this month (``now``'s, in UTC) on the connection a request
    will use, against their allowance there.

    The connection is ``connection_id``, else the one ``model`` resolves to. Without
    either, the most used of the user's metered connections (no limit when none).
    """
    period = usage.period_of(now or datetime.now(timezone.utc))
    if connection_id is None and model:
        connection_id = connection_of(model)
    if not _server():
        return BudgetStatus(period, connection_id=connection_id)
    if connection_id is not None:
        return await _standing(db, user_guid, roles, connection_id, period)
    found = await standings(db, user_guid, roles, now=now)
    if not found:
        return BudgetStatus(period)
    return max(found, key=lambda s: (_RANK[s.state], s.percent or 0.0))


def check(
    db: SyncDatabase,
    user_guid: str,
    roles: Iterable[str] | None,
    *,
    now: datetime | None = None,
    connection_id: str | None = None,
    model: str | None = None,
) -> BudgetStatus:
    """The standing for a new request from synchronous code; raises :class:`BudgetExceeded`
    at 100% of the allowance on the request's connection.

    The connection is ``connection_id``, else the one ``model`` resolves to; when
    neither names one, nothing is refused. A refusal is audited (server mode) with the
    connection, the credits and the allowance, never content.
    """
    if connection_id is None and model:
        connection_id = connection_of(model)
    status = db.call(status_for, user_guid, roles, now=now, connection_id=connection_id)
    if connection_id is not None and status.state == "exceeded":
        from spark.identity import audit

        audit.submit(
            audit.USAGE_REFUSED,
            "failure",
            user_guid=user_guid,
            resource_type="budget",
            resource_id=status.period,
            details={
                "connection_id": connection_id,
                "used": float(status.used),
                "limit": float(status.limit or 0),
                "period": status.period,
            },
        )
        raise BudgetExceeded(status)
    return status
