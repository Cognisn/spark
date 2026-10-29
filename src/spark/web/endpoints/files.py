"""Files endpoint: the signed-in user's file area.

Every route is scoped to the signed-in user: another user's file is a 404,
never a 403, so its existence is not revealed. Uploads are refused over the
maximum upload size (413) or the user's quota (507), and nothing is stored.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from spark import files as files_pkg
from spark.database import conversations as convdb
from spark.database import files as files_db
from spark.db.engine import Row
from spark.files.area import FileArea, FileAreaNotConfigured
from spark.files.paths import PathRefused
from spark.files.store import UploadTooLarge
from spark.web.db import database, sync_database

__all__ = ["router"]

router = APIRouter(prefix="/files")

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()+-]")
_MULTIPART_OVERHEAD = 64 * 1024
_CHUNK = 256 * 1024


def _user_guid(request: Request) -> str | None:
    """The signed-in user (the identity layer sets ``request.state.user_guid``)."""
    user = getattr(request.state, "user_guid", None) or getattr(
        request.app.state, "user_guid", None
    )
    return str(user) if user else None


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _safe_name(name: str | None) -> str:
    """A plain file name from an uploaded name: no directories, streams or odd characters."""
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = _UNSAFE.sub("_", base).strip(" .")[:120].strip(" .")
    return cleaned or "upload"


def _describe(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "key": row["storage_key"],
        "filename": row["filename"],
        "size_bytes": row["size_bytes"],
        "mime_type": row["mime_type"],
        "conversation_id": row["conversation_id"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _area(request: Request) -> FileArea:
    return files_pkg.file_area(sync_database(request))


@router.get("/api")
async def list_files(request: Request) -> Response:
    user = _user_guid(request)
    if user is None:
        return _error(401, "Sign in to see your files.")
    try:
        limits = files_pkg.file_limits()
        files_pkg.file_store()
    except FileAreaNotConfigured as exc:
        return _error(503, f"File storage is not available: {exc}.")
    db = database(request)
    rows = await files_db.list_user_files(db, user)
    return JSONResponse(
        {
            "files": [_describe(r) for r in rows],
            "usage_bytes": await files_db.get_usage(db, user),
            "quota_bytes": limits.quota_bytes,
            "max_upload_bytes": limits.max_upload_bytes,
        }
    )


@router.post("/api")
async def upload_file(request: Request) -> Response:
    user = _user_guid(request)
    if user is None:
        return _error(401, "Sign in to upload files.")
    try:
        area = _area(request)
    except FileAreaNotConfigured as exc:
        return _error(503, f"File storage is not available: {exc}.")

    max_bytes = area.limits().max_upload_bytes
    declared = request.headers.get("content-length", "")
    if max_bytes is not None and declared.isdigit():
        if int(declared) > max_bytes + _MULTIPART_OVERHEAD:
            return _error(413, str(UploadTooLarge(max_bytes)))

    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return _error(400, "No file was uploaded.")

    conversation_id: int | None = None
    raw_cid = form.get("conversation_id")
    if isinstance(raw_cid, str) and raw_cid.strip():
        if not raw_cid.strip().isdigit():
            return _error(400, "conversation_id must be a number.")
        conversation_id = int(raw_cid)
        owned = await convdb.get_conversation(database(request), conversation_id, user)
        if owned is None:
            return _error(404, "Conversation not found.")

    filename = _safe_name(upload.filename)
    key = f"uploads/{uuid.uuid4().hex}/{filename}"
    try:
        file_id = await asyncio.to_thread(
            area.save,
            user,
            key,
            upload.file,
            filename=filename,
            mime_type=upload.content_type,
            conversation_id=conversation_id,
        )
    except UploadTooLarge as exc:
        return _error(413, str(exc))
    except files_db.QuotaExceeded as exc:
        return _error(507, str(exc))
    except PathRefused as exc:
        return _error(400, str(exc))
    finally:
        await upload.close()

    row = await files_db.get_user_file(database(request), user, file_id)
    assert row is not None
    return JSONResponse({"file": _describe(row)}, status_code=201)


def _iterate(handle: Any) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/api/{file_id}")
async def download_file(request: Request, file_id: int) -> Response:
    user = _user_guid(request)
    if user is None:
        return _error(401, "Sign in to download files.")
    row = await files_db.get_user_file(database(request), user, file_id)
    if row is None:
        return _error(404, "File not found.")
    try:
        store = files_pkg.file_store()
        handle = await asyncio.to_thread(store.open, user, str(row["storage_key"]))
    except FileAreaNotConfigured as exc:
        return _error(503, f"File storage is not available: {exc}.")
    except (FileNotFoundError, PathRefused):
        return _error(404, "File not found.")
    return StreamingResponse(
        _iterate(handle),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _disposition(str(row["filename"])),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/api/{file_id}")
async def delete_file(request: Request, file_id: int) -> Response:
    user = _user_guid(request)
    if user is None:
        return _error(401, "Sign in to delete files.")
    try:
        store = files_pkg.file_store()
    except FileAreaNotConfigured as exc:
        return _error(503, f"File storage is not available: {exc}.")
    row = await files_db.delete_user_file(database(request), user, file_id)
    if row is None:
        return _error(404, "File not found.")
    try:
        await asyncio.to_thread(store.delete, user, str(row["storage_key"]))
    except PathRefused:
        pass
    return JSONResponse({"deleted": file_id})
