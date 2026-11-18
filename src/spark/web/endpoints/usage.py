"""Usage and monthly credit allowances on the web (A14, M6 to M11).

* ``/api/usage/me``: the signed-in user's own standing this month, in credits:
  on one connection (``?connection=<id>`` or ``?model=<connection/model>``), else
  their most used metered connection, with every metered connection of theirs
  under ``connections`` (the model picker shows what is left on each). The chat,
  debate and panel pages warn from 80% and say at 100% that new requests on that
  connection are refused.
* ``/admin/usage``: usage by month, user, connection and model (the four token
  counts and credits), totals per connection, and each user's standing on each
  metered connection; ``/admin/usage/export.csv`` exports the same usage. Both need
  ``spark.usage.admin``, which only Administrators hold, and both are audited.

Allowances are set on each connection's assignments (the connections screens),
not here. Users are named by email address on a server. Personal mode meters no
credits (M3): its view shows tokens and credits worked out at each connection's
rates, without standings.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from spark.database import usage
from spark.db.engine import Executor
from spark.identity import audit
from spark.identity.access import SparkIdentity, current_identity, require_permission
from spark.identity.permissions import USAGE_ADMIN
from spark.usage import budgets
from spark.web.db import database

__all__ = ["router"]

router = APIRouter()

MY_USAGE_PATH = "/api/usage/me"
USAGE_PATH = "/admin/usage"
EXPORT_PATH = "/admin/usage/export.csv"

LOCAL_USER = "Local user"
NO_CONNECTION = "(no connection)"
ALL_MONTHS = "all"
BAD_MONTH = "Choose a month as YYYY-MM, or all months."

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# A CSV cell starting with one of these is read as a formula by spreadsheets.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

administrator = require_permission(USAGE_ADMIN)


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


@dataclass(frozen=True, slots=True)
class Person:
    """A user the view names: their SPARK guid and how they are shown."""

    user_guid: str
    label: str
    warden_user_id: str = ""


async def _people(request: Request, db: Executor) -> list[Person]:
    """Every user the view names: the warden users with a SPARK guid, or the local user."""
    if not _server():
        guid = str(getattr(request.app.state, "user_guid", "") or "default")
        return [Person(guid, LOCAL_USER)]
    from spark.web.endpoints.identity import identity_runtime

    users_api = identity_runtime(request).identity.users
    emails: dict[str, str] = {}
    after: str | None = None
    while True:
        page = await users_api.list(limit=200, after=after)
        emails.update({str(u.id): u.email for u in page})
        if len(page) < 200:
            break
        after = users_api.cursor(page[-1])
    linked = await usage.warden_users_all_users(db)
    people = [
        Person(guid, emails.get(warden_id, guid), warden_id) for guid, warden_id in linked.items()
    ]
    return sorted(people, key=lambda p: p.label.lower())


async def _roles_of(request: Request, person: Person) -> frozenset[str]:
    """A user's roles now (none in personal mode, which meters no credits)."""
    if not _server() or not person.warden_user_id:
        return frozenset()
    from spark.web.endpoints.identity import identity_runtime

    try:
        warden_user = uuid.UUID(person.warden_user_id)
    except ValueError:
        return frozenset()
    roles = await identity_runtime(request).identity.roles.roles_of(warden_user)
    return frozenset(roles)


class _BadMonthError(Exception):
    """A month that is neither YYYY-MM nor all months."""


def _month(request: Request) -> str | None:
    """The month asked for: a period, None for every month; 400 for anything else."""
    value = request.query_params.get("month", "").strip()
    if not value:
        return usage.period_of(datetime.now(timezone.utc))
    if value == ALL_MONTHS:
        return None
    if not _MONTH.fullmatch(value):
        raise _BadMonthError(value)
    return value


def _credits(value: Decimal) -> str:
    """Credits to two places for the page (the CSV keeps all six)."""
    return f"{value:,.2f}"


# -- For the turn endpoints -----------------------------------------------------------------


def request_roles(request: Request) -> frozenset[str] | None:
    """The signed-in user's roles, which decide their allowances (None when signed out)."""
    identity = current_identity(request)
    return identity.roles if identity is not None else None


def refused_response(refused: budgets.BudgetExceeded) -> JSONResponse:
    """A request refused at 100% of the user's allowance: 429 with the message and standing."""
    return JSONResponse(
        {"error": str(refused), "budget": refused.status.as_dict()}, status_code=429
    )


def refused_event(refused: budgets.BudgetExceeded) -> dict[str, str]:
    """The event a stream sends when its turn is refused at 100% of the user's allowance."""
    import json

    return {
        "event": "budget_exceeded",
        "data": json.dumps({"message": str(refused), **refused.status.as_dict()}),
    }


# -- The user's own standing ---------------------------------------------------------------


@router.get(MY_USAGE_PATH)
async def my_usage(request: Request) -> JSONResponse:
    """The signed-in user's credits this month: on one connection, and on each of theirs."""
    from spark.identity.access import current_user_guid

    identity = current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="not signed in")
    db = database(request)
    user_guid = current_user_guid(request)
    connection = request.query_params.get("connection", "").strip() or None
    model = request.query_params.get("model", "").strip() or None
    status = await budgets.status_for(
        db, user_guid, identity.roles, connection_id=connection, model=model
    )
    listed = await budgets.standings(db, user_guid, identity.roles)
    body = {
        **status.as_dict(),
        "connections": {s.connection_id: s.as_dict() for s in listed if s.connection_id},
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


# -- The administrator's view ---------------------------------------------------------------


async def _connection_names(db: Executor) -> dict[str, str]:
    from spark.database.connections import list_connections

    return {c.id: c.name for c in await list_connections(db)}


def _row(raw: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    connection_id = str(raw.get("connection_id") or "")
    counts = {key: int(raw[key]) for key in _TOKEN_KEYS}
    return {
        "connection_id": connection_id,
        "connection": names.get(connection_id, connection_id) or NO_CONNECTION,
        "requests": int(raw["requests"]),
        **counts,
        "total_tokens": sum(counts.values()),
        "credits": raw["credits"],
    }


async def _report(
    request: Request, db: Executor, period: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Person], dict[str, str]]:
    people = await _people(request, db)
    labels = {p.user_guid: p.label for p in people}
    names = await _connection_names(db)
    rows = [
        {
            "period": r["period"],
            "user": labels.get(str(r["user_guid"]), str(r["user_guid"])),
            "user_guid": str(r["user_guid"]),
            "model": str(r["model_id"] or ""),
            **_row(r, names),
        }
        for r in await usage.report_all_users(db, period=period)
    ]
    totals = [_row(r, names) for r in await usage.connection_totals(db, period=period)]
    return rows, totals, people, names


async def _usage_page(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    from spark.web.endpoints.account import screen

    db = database(request)
    current = usage.period_of(datetime.now(timezone.utc))
    try:
        period = _month(request)
    except _BadMonthError:
        period, error, status_code = current, BAD_MONTH, 400
    rows, totals, people, _names = await _report(request, db, period)
    standings = []
    for person in people:
        for status in await budgets.standings(
            db, person.user_guid, await _roles_of(request, person)
        ):
            standings.append({"person": person, "status": status})
    months = sorted({current, *await usage.periods_all_users(db)}, reverse=True)
    return screen(
        request,
        "admin_usage.html",
        {
            "rows": rows,
            "totals": totals,
            "standings": standings,
            "months": months,
            "month": period or ALL_MONTHS,
            "server": _server(),
            "credits": _credits,
            "error": error,
        },
        status_code=status_code,
    )


@router.get(USAGE_PATH, response_class=HTMLResponse)
async def usage_page(request: Request, _: SparkIdentity = Depends(administrator)) -> HTMLResponse:
    """Usage by month, user, connection and model, totals, and each user's standing."""
    response = await _usage_page(request)
    if response.status_code == 200:
        await audit.emit_request(
            request,
            audit.USAGE_VIEWED,
            resource_type="usage",
            resource_id=request.query_params.get("month", "").strip()
            or usage.period_of(datetime.now(timezone.utc)),
        )
    return response


def _cell(value: Any) -> Any:
    """A CSV value a spreadsheet will not run as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


@router.get(EXPORT_PATH)
async def export_usage(request: Request, _: SparkIdentity = Depends(administrator)) -> Response:
    """The usage of one month (or every month) as CSV: tokens and credits."""
    try:
        period = _month(request) if request.query_params.get("month") else None
    except _BadMonthError:
        raise HTTPException(status_code=400, detail=BAD_MONTH) from None
    rows, _totals, _people_listed, _names = await _report(request, database(request), period)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    columns = [
        "month",
        "user",
        "user_guid",
        "connection",
        "connection_id",
        "model",
        "requests",
        *_TOKEN_KEYS,
        "total_tokens",
        "credits",
    ]
    writer.writerow(columns)
    keys = ["period", *columns[1:]]
    for row in rows:
        writer.writerow([_cell(str(row[key]) if key == "credits" else row[key]) for key in keys])
    name = period or ALL_MONTHS
    await audit.emit_request(
        request, audit.EXPORT_DOWNLOADED, resource_type="usage", resource_id=name
    )
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="spark-usage-{name}.csv"',
            "Cache-Control": "no-store",
        },
    )
