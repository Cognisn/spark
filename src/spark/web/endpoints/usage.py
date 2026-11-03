"""Usage and monthly token budgets on the web (A14).

* ``/api/usage/me``: the signed-in user's own standing this month (used,
  limit, percent, state, when it resets), which the chat, debate and panel
  pages show as a warning from 80%.
* ``/admin/usage``: usage by month, user and model, each user's standing, and
  the budget forms; ``/admin/usage/export.csv`` exports the same usage. Both,
  and both forms, need ``spark.usage.admin``, which only Administrators hold.
  Viewing, exporting and changing a budget are audited.

Users are named by email address on a server. Personal mode has one local
user, who holds every permission: role budgets do not apply there, so only the
local user's own budget is offered.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from spark.database import usage
from spark.db.engine import Executor
from spark.identity import audit
from spark.identity.access import SparkIdentity, current_identity, require_permission
from spark.identity.permissions import ROLES, USAGE_ADMIN
from spark.usage import budgets
from spark.web.db import database

__all__ = ["router"]

router = APIRouter()

MY_USAGE_PATH = "/api/usage/me"
USAGE_PATH = "/admin/usage"
EXPORT_PATH = "/admin/usage/export.csv"
ROLE_BUDGET_PATH = "/admin/usage/budgets/roles"
USER_BUDGET_PATH = "/admin/usage/budgets/users"

LOCAL_USER = "Local user"
ALL_MONTHS = "all"
BAD_MONTH = "Choose a month as YYYY-MM, or all months."
BAD_BUDGET = "A budget is a positive whole number of tokens, 'unlimited', or empty to remove it."
UNKNOWN_ROLE = "There is no such role."
DONE = {"role": "The role's budget is saved.", "user": "The user's budget is saved."}

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# A CSV cell starting with one of these is read as a formula by spreadsheets.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

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
    """A user's roles now (none in personal mode, where roles set no budget)."""
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


def _parse_budget(value: str) -> tuple[bool, int | None]:
    """A budget field: (clear, tokens). Raises ValueError for anything not allowed."""
    text = value.strip().replace(",", "").replace(" ", "")
    if not text:
        return True, None
    if text.lower() == "unlimited":
        return False, None
    if not text.isdigit() or int(text) < 1:
        raise ValueError(BAD_BUDGET)
    return False, int(text)


# -- For the turn endpoints -----------------------------------------------------------------


def request_roles(request: Request) -> frozenset[str] | None:
    """The signed-in user's roles, which decide their budget (None when no one is signed in)."""
    identity = current_identity(request)
    return identity.roles if identity is not None else None


def refused_response(refused: budgets.BudgetExceeded) -> JSONResponse:
    """A request refused at 100% of the user's budget: 429 with the message and the standing."""
    return JSONResponse(
        {"error": str(refused), "budget": refused.status.as_dict()}, status_code=429
    )


def refused_event(refused: budgets.BudgetExceeded) -> dict[str, str]:
    """The event a stream sends when its turn is refused at 100% of the user's budget."""
    import json

    return {
        "event": "budget_exceeded",
        "data": json.dumps({"message": str(refused), **refused.status.as_dict()}),
    }


# -- The user's own standing ---------------------------------------------------------------


@router.get(MY_USAGE_PATH)
async def my_usage(request: Request) -> JSONResponse:
    """The signed-in user's usage this month against their budget."""
    from spark.identity.access import current_user_guid

    identity = current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="not signed in")
    status = await budgets.status_for(database(request), current_user_guid(request), identity.roles)
    return JSONResponse(status.as_dict(), headers={"Cache-Control": "no-store"})


# -- The administrator's view ---------------------------------------------------------------


async def _report(
    request: Request, db: Executor, period: str | None
) -> tuple[list[dict[str, Any]], list[Person]]:
    people = await _people(request, db)
    labels = {p.user_guid: p.label for p in people}
    rows = [
        {
            "period": r["period"],
            "user": labels.get(str(r["user_guid"]), str(r["user_guid"])),
            "user_guid": str(r["user_guid"]),
            "model": str(r["model_id"] or ""),
            "requests": r["requests"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "total_tokens": r["input_tokens"] + r["output_tokens"],
        }
        for r in await usage.report_all_users(db, period=period)
    ]
    return rows, people


async def _usage_page(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    from spark.web.endpoints.account import screen

    db = database(request)
    current = usage.period_of(datetime.now(timezone.utc))
    try:
        period = _month(request)
    except _BadMonthError:
        period, error, status_code = current, BAD_MONTH, 400
    rows, people = await _report(request, db, period)
    overrides = await usage.user_overrides_all_users(db)
    standings = []
    for person in people:
        status = await budgets.status_for(db, person.user_guid, await _roles_of(request, person))
        standings.append(
            {
                "person": person,
                "status": status,
                "has_override": person.user_guid in overrides,
                "override": overrides.get(person.user_guid),
            }
        )
    role_budgets = await usage.role_budgets(db)
    months = sorted({current, *await usage.periods_all_users(db)}, reverse=True)
    return screen(
        request,
        "admin_usage.html",
        {
            "rows": rows,
            "standings": standings,
            "months": months,
            "month": period or ALL_MONTHS,
            "server": _server(),
            "roles": [{"name": name, "budget": role_budgets.get(name)} for name in sorted(ROLES)],
            "error": error,
            "notice": notice,
        },
        status_code=status_code,
    )


@router.get(USAGE_PATH, response_class=HTMLResponse)
async def usage_page(request: Request, _: SparkIdentity = Depends(administrator)) -> HTMLResponse:
    """Usage by month, user and model, with each user's standing and the budgets."""
    response = await _usage_page(request, notice=DONE.get(request.query_params.get("done", "")))
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
    """The usage of one month (or every month) as CSV."""
    try:
        period = _month(request) if request.query_params.get("month") else None
    except _BadMonthError:
        raise HTTPException(status_code=400, detail=BAD_MONTH) from None
    rows, _people_listed = await _report(request, database(request), period)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    columns = [
        "month",
        "user",
        "user_guid",
        "model",
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    ]
    writer.writerow(columns)
    keys = ["period", *columns[1:]]
    for row in rows:
        writer.writerow([_cell(row[key]) for key in keys])
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


# -- Budgets --------------------------------------------------------------------------------


async def _form(request: Request) -> Mapping[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


@router.post(ROLE_BUDGET_PATH)
async def set_role_budget(request: Request, _: SparkIdentity = Depends(administrator)) -> Response:
    """Set or remove a role's monthly budget (server mode; personal mode has no roles)."""
    from spark.web.endpoints.identity import redirect

    if not _server():
        raise HTTPException(status_code=404)
    form = await _form(request)
    role = form.get("role", "")
    if role not in ROLES:
        return await _usage_page(request, error=UNKNOWN_ROLE, status_code=400)
    try:
        # A role is unlimited by having no budget, so "unlimited" removes it too.
        _clear, tokens = _parse_budget(form.get("monthly_tokens", ""))
    except ValueError:
        return await _usage_page(request, error=BAD_BUDGET, status_code=400)
    db = database(request)
    clear = tokens is None
    if tokens is None:
        await usage.clear_role_budget(db, role)
    else:
        await usage.set_role_budget(db, role, tokens)
    await audit.emit_request(
        request,
        audit.USAGE_BUDGET_CHANGED,
        resource_type="role",
        resource_id=role,
        details={"limit": tokens, "removed": clear},
    )
    return redirect(f"{USAGE_PATH}?done=role")


@router.post(USER_BUDGET_PATH)
async def set_user_budget(request: Request, _: SparkIdentity = Depends(administrator)) -> Response:
    """Give a user their own budget, make them unlimited, or let their roles decide again."""
    from spark.web.endpoints.identity import redirect

    form = await _form(request)
    guid = form.get("user_guid", "")
    db = database(request)
    if guid not in {p.user_guid for p in await _people(request, db)}:
        raise HTTPException(status_code=404)
    try:
        clear, tokens = _parse_budget(form.get("monthly_tokens", ""))
    except ValueError:
        return await _usage_page(request, error=BAD_BUDGET, status_code=400)
    if clear:
        await usage.clear_user_override(db, guid)
    else:
        await usage.set_user_override(db, guid, tokens)
    await audit.emit_request(
        request,
        audit.USAGE_BUDGET_CHANGED,
        resource_type="user",
        resource_id=guid,
        details={"limit": tokens, "unlimited": not clear and tokens is None, "removed": clear},
    )
    return redirect(f"{USAGE_PATH}?done=user")
