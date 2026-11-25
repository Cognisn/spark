"""The chart API: what the page draws for a stored chart or diagram, and its data.

* ``GET /charts/api/{file_id}`` answers a chart's ECharts option, built on the server
  by :mod:`spark.charts.build` for the asked theme (``?theme=light`` or ``dark``;
  ``&export=1`` adds the title and background for a saved image), or a diagram's
  Mermaid source. The page never draws anything the model wrote as code: it draws
  this option.
* ``GET /charts/api/{file_id}/data.csv`` answers a chart's data snapshot as CSV, with
  no cell that a spreadsheet would run as a formula. Each download is audited
  (``spark.file.downloaded``) in server mode.

A file is reached as the document preview reaches one: one of the caller's own files,
or a file of a conversation they may read, and then only through that conversation's
access decision (:func:`spark.web.scope.conversation_access`). Anything else, and any
file that is not a chart or a diagram, answers 404. A stored file that no longer
passes the checks answers 422 and is not drawn.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from spark import files as files_pkg
from spark.charts.artefact import Chart, Diagram, csv_bytes, payload, read_artefact
from spark.charts.theme import theme_named
from spark.charts.validate import ChartRefused
from spark.database import files as files_db
from spark.database.scope import Capability
from spark.db.engine import Row
from spark.files.area import FileAreaNotConfigured
from spark.files.paths import PathRefused
from spark.identity import audit
from spark.identity.access import current_user_guid
from spark.preview.service import safe_name
from spark.web.db import database
from spark.web.scope import ConversationAccess, conversation_access, not_found

__all__ = ["ARTEFACT_KINDS", "router"]

router = APIRouter(prefix="/charts")

ARTEFACT_KINDS: Final = frozenset({"chart", "diagram"})
_HEADERS: Final = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def _readable(request: Request, file_id: int) -> Row | Response:
    """The caller's own chart or diagram, or one of a conversation they may read."""
    user = current_user_guid(request)
    row = await files_db.get_readable_file(database(request), user, file_id)
    if row is None or row["kind"] not in ARTEFACT_KINDS:
        return not_found()
    return row


async def _load(row: Row) -> Chart | Diagram | Response:
    """The stored chart or diagram, checked again before anything is drawn."""
    try:
        store = files_pkg.file_store()
        raw = await asyncio.to_thread(_read, store, str(row["user_guid"]), str(row["storage_key"]))
    except FileAreaNotConfigured as exc:
        return JSONResponse({"error": f"File storage is not available: {exc}."}, status_code=503)
    except (FileNotFoundError, PathRefused):
        return not_found()
    try:
        return read_artefact(raw)
    except ChartRefused as exc:
        return JSONResponse({"error": f"This chart cannot be drawn: {exc}"}, status_code=422)


def _read(store: Any, owner: str, key: str) -> bytes:
    with store.open(owner, key) as handle:
        data: bytes = handle.read()
    return data


@router.get("/api/{file_id}")
async def chart_view(request: Request, file_id: int) -> Response:
    """What the page draws: a chart's option for the theme, or a diagram's source."""
    row = await _readable(request, file_id)
    if isinstance(row, Response):
        return row
    if row["conversation_id"] is not None:
        access = await conversation_access(request, int(row["conversation_id"]), Capability.READ)
        if not isinstance(access, ConversationAccess):
            return access
    artefact = await _load(row)
    if isinstance(artefact, Response):
        return artefact
    theme = theme_named(request.query_params.get("theme"))
    export = request.query_params.get("export") in ("1", "true")
    body = payload(artefact, theme, export=export)
    body["file_id"] = int(row["id"])
    return JSONResponse(body, headers=dict(_HEADERS))


@router.get("/api/{file_id}/data.csv")
async def chart_data(request: Request, file_id: int) -> Response:
    """A chart's data snapshot as CSV, for Download data."""
    row = await _readable(request, file_id)
    if isinstance(row, Response):
        return row
    if row["conversation_id"] is not None:
        access = await conversation_access(request, int(row["conversation_id"]), Capability.READ)
        if not isinstance(access, ConversationAccess):
            return access
    artefact = await _load(row)
    if isinstance(artefact, Response):
        return artefact
    if not isinstance(artefact, Chart):
        return not_found()
    data = csv_bytes(artefact)
    await audit.emit_request(
        request,
        audit.FILE_DOWNLOADED,
        resource_type="file",
        resource_id=str(file_id),
        details={
            "size_bytes": len(data),
            "conversation_id": row["conversation_id"],
            "disposition": "chart_data",
        },
    )
    name = f"{safe_name(artefact.title)}.csv"
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={
            **_HEADERS,
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
            ),
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )
