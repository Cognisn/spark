"""The audit trail viewer and export (server mode).

* ``GET /audit`` (``spark.audit.read``): warden's audit events, newest first,
  :data:`PAGE_SIZE` at a time, filtered by actor (an email address or a user
  id), action, outcome and time range (dates, UTC; ``until`` includes its
  day). Actors are shown by email address where they are users.
* ``GET /audit/export`` (``spark.audit.export``): every matching event, in
  chain order, as CSV or NDJSON, through warden's export, which records the
  export itself (filter, actor and client address) before the first line.

An Auditor holds exactly these two permissions and reaches no other
administration. Nothing here writes to the trail except warden's own export
record.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import ValidationError
from warden.audit import AuditQuery, list_events
from warden.audit.export import export

from spark.identity.access import SparkIdentity, require_permission
from spark.identity.permissions import AUDIT_EXPORT, AUDIT_READ
from spark.identity.runtime import IdentityRuntime
from spark.identity.web import source_ip
from spark.web.endpoints.account import screen
from spark.web.endpoints.identity import identity_runtime

router = APIRouter()

AUDIT_PATH = "/audit"
PAGE_SIZE = 50
BAD_FILTER = "A filter is not valid: dates are YYYY-MM-DD and the outcome is success or failure."
_MEDIA = {"csv": "text/csv; charset=utf-8", "ndjson": "application/x-ndjson"}
_DETAILS_SHOWN = 300


class FilterError(ValueError):
    """A filter the viewer cannot use."""


def _day(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise FilterError(value) from None


async def _actor(runtime: IdentityRuntime, value: str) -> str | None:
    """An actor filter: a user's email address becomes their id; anything else is as typed."""
    value = value.strip()
    if not value:
        return None
    if "@" in value:
        user = await runtime.identity.users.get_by_email(value)
        # An unknown address matches nothing (no actor is an email address).
        return str(user.id) if user is not None else value
    return value


async def _query(request: Request, runtime: IdentityRuntime) -> AuditQuery:
    """warden's query for the request's filters; :class:`FilterError` if one is not valid."""
    params = request.query_params
    since, until = _day(params.get("since", "")), _day(params.get("until", ""))
    outcomes: dict[str, Literal["success", "failure"] | None] = {
        "": None,
        "success": "success",
        "failure": "failure",
    }
    typed = params.get("outcome", "")
    if typed not in outcomes:
        raise FilterError(typed)
    outcome = outcomes[typed]
    actions = tuple(a.strip() for a in params.getlist("action") if a.strip())
    try:
        return AuditQuery(
            since=datetime.combine(since, time(), UTC) if since else None,
            until=datetime.combine(until + timedelta(days=1), time(), UTC) if until else None,
            actions=actions,
            actor=await _actor(runtime, params.get("actor", "")),
            outcome=outcome,
        )
    except ValidationError:
        raise FilterError("query") from None


def _filters(request: Request) -> dict[str, str]:
    """The filters as typed, to show them back in the form."""
    params = request.query_params
    return {key: params.get(key, "") for key in ("actor", "action", "outcome", "since", "until")}


async def _emails(runtime: IdentityRuntime, actors: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for actor in actors:
        try:
            user = await runtime.identity.users.get(uuid.UUID(actor))
        except ValueError:
            continue
        if user is not None:
            found[actor] = user.email
    return found


def _details(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= _DETAILS_SHOWN else text[:_DETAILS_SHOWN] + "..."


@router.get(AUDIT_PATH, response_class=HTMLResponse)
async def audit_page(
    request: Request, identity: SparkIdentity = Depends(require_permission(AUDIT_READ))
) -> HTMLResponse:
    runtime = identity_runtime(request)
    filters = _filters(request)
    context: dict[str, Any] = {
        "filters": filters,
        "newest": urlencode({k: v for k, v in filters.items() if v}),
        "rows": [],
        "actors": {},
        "older": None,
        "may_export": AUDIT_EXPORT in identity.permissions,
    }
    before = request.query_params.get("before_id", "")
    try:
        query = await _query(request, runtime)
        before_id = int(before) if before else None
    except (FilterError, ValueError):
        return screen(request, "audit.html", {**context, "error": BAD_FILTER}, status_code=400)
    page = await list_events(runtime.recorder.db, query, limit=PAGE_SIZE, before_id=before_id)
    rows: list[dict[str, str | int]] = [
        {
            "id": row.id,
            "occurred_at": row.occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "action": row.action,
            "outcome": row.outcome,
            "actor": row.actor or "",
            "resource": f"{row.resource_type}:{row.resource_id}" if row.resource_type else "",
            "source_ip": str(row.source_ip) if row.source_ip is not None else "",
            "details": _details(dict(row.details)),
        }
        for row in page.rows
    ]
    context["rows"] = rows
    context["actors"] = await _emails(runtime, {str(r["actor"]) for r in rows if r["actor"]})
    if page.next_before_id is not None:
        older = {k: v for k, v in filters.items() if v}
        older["before_id"] = str(page.next_before_id)
        context["older"] = urlencode(older)
    return screen(request, "audit.html", context)


@router.get(AUDIT_PATH + "/export")
async def audit_export(
    request: Request, identity: SparkIdentity = Depends(require_permission(AUDIT_EXPORT))
) -> StreamingResponse:
    """Every matching event as CSV or NDJSON; warden records the export first."""
    runtime = identity_runtime(request)
    fmt = request.query_params.get("format", "csv")
    if fmt not in _MEDIA:
        raise HTTPException(status_code=400, detail="format must be csv or ndjson")
    try:
        query = await _query(request, runtime)
    except FilterError:
        raise HTTPException(status_code=400, detail=BAD_FILTER) from None
    chosen: Literal["csv", "ndjson"] = "csv" if fmt == "csv" else "ndjson"
    address = source_ip(runtime, request)
    lines: AsyncIterator[str] = await export(
        runtime.recorder,
        query,
        chosen,
        actor=identity.warden_user_id,
        source_ip=str(address) if address is not None else None,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return StreamingResponse(
        lines,
        media_type=_MEDIA[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="spark-audit-{stamp}.{fmt}"',
            "Cache-Control": "no-store",
        },
    )
