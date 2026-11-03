"""User bundles on the web (A15): the personal export, and the server administrator's import.

* ``GET /bundles/export`` (personal mode only): downloads the local user's bundle
  (:mod:`spark.bundles.exporter`), never cached. A server answers 404.
* ``/admin/bundles`` (server mode only, ``spark.bundles.import``, which only the
  Administrator role holds): ``GET`` shows the upload form with the users to
  import for; ``POST`` takes the bundle (multipart ``bundle``) and the target
  warden user (``user_id``), imports it and shows the report on screen.

The upload is refused while it streams once it passes :data:`LIMITS`, then
checked by :func:`spark.bundles.reader.open_bundle` (size, containment,
version, integrity, credentials) before anything is written. The import and its
audit record (``spark.bundle.imported``) commit together; a refused import is
audited as a failure with a reason code only. Personal mode has no import page.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import AsyncGenerator, Iterator
from typing import IO, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from warden.identity import UserStatus

import spark
from spark import files as files_pkg
from spark import mode
from spark.bundles import format as bf
from spark.bundles.exporter import export_bundle
from spark.bundles.importer import (
    AlreadyImported,
    ImportRefused,
    ImportReport,
    ImportTarget,
    OverQuota,
    import_bundle,
)
from spark.bundles.personal import bundle_filename, personal_sources
from spark.bundles.reader import BundleRefused, BundleTooLarge, Limits, open_bundle
from spark.database.user_identities import user_guid_for
from spark.db.engine import Connection
from spark.files.area import FileAreaNotConfigured
from spark.identity import audit
from spark.identity.access import SparkIdentity, require_permission
from spark.identity.permissions import BUNDLES_IMPORT
from spark.identity.web import source_ip
from spark.web.db import database
from spark.web.endpoints.account import screen
from spark.web.endpoints.identity import identity_runtime

__all__ = ["LIMITS", "router"]

router = APIRouter()

IMPORT_PATH = "/admin/bundles"
# What an import accepts; the upload is cut off once it passes the bundle limit.
LIMITS = Limits()
_MULTIPART_OVERHEAD = 64 * 1024
_SPOOL = 32 << 20
_CHUNK = 1 << 20
_USERS_SHOWN = 500

importer = require_permission(BUNDLES_IMPORT)


def _server() -> bool:
    try:
        return mode.is_server()
    except RuntimeError:  # a process that never configured its mode is personal
        return False


def server_only() -> None:
    if not _server():
        raise HTTPException(status_code=404)


def personal_only() -> None:
    if _server():
        raise HTTPException(status_code=404)


# -- Export (personal mode) -------------------------------------------------------------


def _iterate(handle: IO[bytes]) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


@router.get("/bundles/export", dependencies=[Depends(personal_only)])
async def export(request: Request) -> Response:
    """The local user's bundle, as a download."""
    user = getattr(request.app.state, "user_guid", None)
    if not user:
        raise HTTPException(status_code=401)
    ctx = request.app.state.ctx
    sources = personal_sources(ctx.settings, ctx.secrets)
    out = tempfile.SpooledTemporaryFile(max_size=_SPOOL)
    try:
        report = await export_bundle(
            database(request), str(user), out, sources, spark_version=spark.__version__
        )
    except BaseException:
        out.close()
        raise
    out.seek(0)
    return StreamingResponse(
        _iterate(out),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle_filename(report.bundle_id)}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# -- Import (server mode, administrators) -------------------------------------------------


class _TooLarge(Exception):
    pass


async def _capped(request: Request, cap: int) -> AsyncGenerator[bytes, None]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _TooLarge
        yield chunk


async def _users(request: Request) -> list[Any]:
    users = await identity_runtime(request).identity.users.list(
        status=UserStatus.ACTIVE, limit=_USERS_SHOWN
    )
    return sorted(users, key=lambda u: str(u.email).lower())


async def _page(
    request: Request,
    *,
    report: ImportReport | None = None,
    target: Any = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return screen(
        request,
        "admin_bundles.html",
        {
            "users": await _users(request),
            "format_version": bf.FORMAT_VERSION,
            "limit_mb": LIMITS.max_bundle_bytes >> 20,
            "report": None if report is None else report.summary(),
            "target": target,
            "error": error,
        },
        status_code=status_code,
    )


async def _refused(
    request: Request,
    reason: str,
    message: str,
    status_code: int,
    target_id: str | None = None,
) -> HTMLResponse:
    await audit.emit_request(
        request,
        audit.BUNDLE_IMPORTED,
        "failure",
        resource_type="user",
        resource_id=target_id,
        details={"reason": reason},
    )
    return await _page(request, error=message, status_code=status_code)


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


@router.get(IMPORT_PATH, response_class=HTMLResponse, dependencies=[Depends(server_only)])
async def import_form(request: Request, _: SparkIdentity = Depends(importer)) -> HTMLResponse:
    return await _page(request)


@router.post(IMPORT_PATH, dependencies=[Depends(server_only)])
async def import_upload(request: Request, identity: SparkIdentity = Depends(importer)) -> Response:
    """Import an uploaded bundle for the chosen user and show the report."""
    cap = LIMITS.max_bundle_bytes + _MULTIPART_OVERHEAD
    too_large = f"The bundle is larger than {LIMITS.max_bundle_bytes >> 20} MB, the import limit."
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        return await _refused(request, "too_large", too_large, 413)
    try:
        form = await MultiPartParser(
            request.headers, _capped(request, cap), max_files=1, max_fields=8
        ).parse()
    except _TooLarge:
        return await _refused(request, "too_large", too_large, 413)
    except MultiPartException:
        return await _refused(request, "bad_upload", "The upload could not be read.", 400)
    upload = form.get("bundle")
    try:
        if not isinstance(upload, UploadFile):
            return await _refused(request, "no_bundle", "Choose a bundle file to upload.", 400)
        runtime = identity_runtime(request)
        user_id = form.get("user_id")
        try:
            target = await runtime.identity.users.get(uuid.UUID(str(user_id)))
        except ValueError:
            target = None
        if target is None:
            return await _refused(request, "no_user", "Choose a user to import for.", 400)
        target_id = str(target.id)
        try:
            bundle = await asyncio.to_thread(open_bundle, upload.file, LIMITS)
        except BundleTooLarge as exc:
            return await _refused(request, "too_large", _sentence(str(exc)), 413, target_id)
        except BundleRefused as exc:
            return await _refused(request, "bundle_refused", _sentence(str(exc)), 400, target_id)
        db = database(request)
        try:
            store = files_pkg.file_store()
            quota = files_pkg.file_limits().quota_bytes
        except FileAreaNotConfigured as exc:
            bundle.close()
            return await _refused(request, "no_storage", _sentence(str(exc)), 503, target_id)
        servers = _mcp_servers()
        address = source_ip(runtime, request)

        async def record(conn: Connection, report: ImportReport) -> None:
            await audit.record_in(
                conn,
                audit.BUNDLE_IMPORTED,
                user_guid=identity.user_guid or None,
                actor=identity.warden_user_id,
                resource_type="user",
                resource_id=target_id,
                source_ip=None if address is None else str(address),
                details={
                    "bundle_id": report.bundle_id,
                    "format_version": report.format_version,
                    "rows": report.total_rows,
                    "dropped": len(report.dropped),
                    "files": report.imported.get("user_files", 0),
                },
            )

        try:
            with bundle:
                guid = await user_guid_for(db, target_id)
                report = await import_bundle(
                    db,
                    bundle,
                    ImportTarget(
                        user_guid=guid, store=store, quota_bytes=quota, mcp_servers=servers
                    ),
                    within=record,
                )
        except AlreadyImported as exc:
            return await _refused(request, "already_imported", _sentence(str(exc)), 409, target_id)
        except OverQuota as exc:
            return await _refused(request, "quota", _sentence(str(exc)), 507, target_id)
        except BundleRefused as exc:
            return await _refused(request, "bundle_refused", _sentence(str(exc)), 400, target_id)
        except ImportRefused as exc:
            return await _refused(request, "refused", _sentence(str(exc)), 400, target_id)
        return await _page(request, report=report, target=target)
    finally:
        if isinstance(upload, UploadFile):
            await upload.close()


def _mcp_servers() -> list[Any]:
    """The MCP servers this server offers (its instance setting)."""
    from spark.instance_settings import accessor

    try:
        servers = accessor.get("mcp.servers")
    except (RuntimeError, KeyError):
        return []
    return list(servers) if isinstance(servers, list) else []
