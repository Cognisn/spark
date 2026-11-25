"""The document preview's routes: a conversation's files for its members, and the frame.

* ``GET /preview/api/{conversation_id}/files/{file_id}`` serves a file of the
  conversation (:func:`spark.database.files.get_conversation_file`) to anyone the
  conversation's access decision lets read it (:func:`spark.web.scope.conversation_access`):
  every member of a shared conversation, and nobody else (404). ``?download=1``
  answers it as an attachment for Download. The chat page fetches the bytes and hands
  them to the frame; the response never renders as a page of SPARK's: an inline answer
  is given only for a short list of passive types, everything else is
  ``application/octet-stream``, always with ``nosniff`` and a sandboxing Content
  Security Policy. Each answer is audited (``spark.file.downloaded``) in server mode.
* ``GET /preview/frame`` is the renderer page (``preview_frame.html``). The pane loads
  it in an ``<iframe sandbox="allow-scripts">``, so it runs in an opaque origin with
  no access to SPARK's cookies, storage or pages, and this route's policy lets it load
  only SPARK's own preview scripts, make no network request of its own
  (``connect-src 'none'``) and show only images it made itself (``blob:`` and
  ``data:``). The document arrives by ``postMessage`` from the pane.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, Final
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from spark import files as files_pkg
from spark.database import files as files_db
from spark.database.scope import Capability
from spark.files.area import FileAreaNotConfigured
from spark.files.paths import PathRefused
from spark.identity import audit
from spark.preview.kinds import base_type
from spark.web.db import database
from spark.web.scope import ConversationAccess, conversation_access

__all__ = ["FRAME_POLICY", "FILE_POLICY", "INLINE_TYPES", "router"]

router = APIRouter(prefix="/preview")

# The frame: SPARK's own scripts (and the PDF renderer, which the pane hands over as
# text and the frame loads from blob: URLs), inline styles for the Word renderer's
# document styles, images and fonts the frame made itself, and nothing else.
FRAME_POLICY: Final = (
    "default-src 'none'; script-src 'self' blob:; worker-src blob:; "
    "style-src 'self' 'unsafe-inline'; img-src blob: data:; font-src blob: data:; "
    "connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; "
    "child-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)
# A file answer, should a browser ever open one directly: no script, no page of SPARK's.
FILE_POLICY: Final = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
# The types answered as themselves; any other is application/octet-stream.
INLINE_TYPES: Final = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "text/plain",
        "text/csv",
        "text/markdown",
        "application/json",
    }
)
_CHUNK: Final = 256 * 1024
_COMMON: Final = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _disposition(kind: str, filename: str) -> str:
    fallback = "".join(
        c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename
    ).strip()
    return f"{kind}; filename=\"{fallback or 'document'}\"; filename*=UTF-8''{quote(filename)}"


def _iterate(handle: Any) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


@router.get("/frame")
async def preview_frame(request: Request) -> HTMLResponse:
    """The sandboxed renderer page; it has no data of its own."""
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request, "preview_frame.html", {}
    )
    response.headers["Content-Security-Policy"] = FRAME_POLICY
    response.headers["Cache-Control"] = "no-store"
    for name, value in _COMMON.items():
        response.headers[name] = value
    return response


@router.get("/api/{conversation_id}/files/{file_id}")
async def preview_file(request: Request, conversation_id: int, file_id: int) -> Response:
    """A file of the conversation, for a member to preview (or download)."""
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    row = await files_db.get_conversation_file(
        database(request), access.user_guid, conversation_id, file_id
    )
    if row is None:
        return _error(404, "File not found.")
    try:
        store = files_pkg.file_store()
        handle = await asyncio.to_thread(store.open, str(row["user_guid"]), str(row["storage_key"]))
    except FileAreaNotConfigured as exc:
        return _error(503, f"File storage is not available: {exc}.")
    except (FileNotFoundError, PathRefused):
        return _error(404, "File not found.")
    download = request.query_params.get("download") in ("1", "true")
    await audit.emit_request(
        request,
        audit.FILE_DOWNLOADED,
        resource_type="file",
        resource_id=str(file_id),
        details={
            "size_bytes": row["size_bytes"],
            "conversation_id": conversation_id,
            "disposition": "attachment" if download else "preview",
        },
    )
    media = base_type(row["mime_type"])
    served = media if media in INLINE_TYPES and not download else "application/octet-stream"
    return StreamingResponse(
        _iterate(handle),
        media_type=served,
        headers={
            **_COMMON,
            "Content-Disposition": _disposition(
                "attachment" if download else "inline", str(row["filename"])
            ),
            "Content-Security-Policy": FILE_POLICY,
            "Cache-Control": "private, no-store",
        },
    )
