"""Skills management endpoints: list, toggle, view, import, download, delete."""

from __future__ import annotations

import io
import logging
import shutil
import stat
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from spark import mode
from spark.identity.access import current_user_guid, require_permission
from spark.identity.permissions import SETTINGS_INSTANCE
from spark.instance_settings import access
from spark.web.db import database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills")

_IMPORT_ZIP_MAX = 10 * 1024 * 1024

# Imported skills live in one directory every user of the instance shares, so
# importing, deleting and reloading them need spark.settings.instance (an
# administrator's; the interim default of question 20). Turning a skill on or
# off is each user's own. Personal mode's local operator holds every permission.
_INSTANCE_SKILLS = [Depends(require_permission(SETTINGS_INSTANCE))]


def _manager(request: Request) -> Any:
    m = getattr(request.app.state, "skills_manager", None)
    if m is not None:
        return m
    from spark.skills.manager import get_skills_manager

    return get_skills_manager()


@router.get("", response_class=HTMLResponse)
async def skills_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "skills.html")


def _server() -> bool:
    """Server mode? (False when the mode was never configured, as in some tests.)"""
    try:
        return mode.is_server()
    except RuntimeError:
        return False


@router.get("/api/list")
async def list_skills(request: Request) -> JSONResponse:
    """All discovered skills (including invalid), joined with enable state.

    ``enabled`` is the user's own switch. ``instance_enabled`` is the administrator's
    (``skills.disabled_builtin``, built-in skills only): a skill the instance has
    switched off is offered to nobody. ``can_switch_builtin`` says whether this user may
    change it, and ``server`` which setting endpoint saves it (``/settings/api/instance``
    on a server, ``/settings/api/save`` in personal mode); both check the administrator
    again on the server.
    """
    from spark.database import skills as skills_db

    manager = _manager(request)
    switched_off = skills_db.disabled_builtin()
    states = await skills_db.get_skill_states(database(request), current_user_guid(request))
    out = []
    for skill in manager.list_skills(include_invalid=True):
        out.append(
            {
                "name": skill["name"],
                "description": skill["description"],
                "source": skill["source"],
                "valid": skill["valid"],
                "error": skill["error"],
                "enabled": states.get(skill["name"], True),
                "instance_enabled": not (
                    skill["source"] == "bundled" and skill["name"] in switched_off
                ),
            }
        )
    return JSONResponse(
        {
            "skills": out,
            "server": _server(),
            "can_switch_builtin": access.access_check().is_administrator(request),
            "setting": skills_db.SETTING_DISABLED_BUILTIN,
            "switched_off": sorted(switched_off),
        }
    )


@router.post("/api/toggle")
async def toggle_skill(request: Request) -> JSONResponse:
    from spark.database import skills as skills_db

    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    await skills_db.set_skill_enabled(
        database(request), name, bool(data.get("enabled", True)), current_user_guid(request)
    )
    return JSONResponse({"status": "ok"})


@router.get("/api/view")
async def view_skill(request: Request, name: str) -> JSONResponse:
    manager = _manager(request)
    skill = manager.get_skill(name)
    if not skill:
        return JSONResponse({"error": "Unknown skill"}, status_code=404)
    return JSONResponse(
        {
            "name": skill["name"],
            "description": skill["description"],
            "source": skill["source"],
            "body": manager.get_body(name),
            "resources": manager.list_resources(name),
        }
    )


def _validate_zip(zf: zipfile.ZipFile) -> tuple[str | None, str | None]:
    """Return (top_level_folder, error). Rejects unsafe entries."""
    top_levels = set()
    for info in zf.infolist():
        name = info.filename
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            return None, "Zip contains unsafe paths"
        if stat.S_ISLNK(info.external_attr >> 16):
            return None, "Zip contains symlinks"
        if p.parts:
            top_levels.add(p.parts[0])
    top_levels.discard("__MACOSX")
    if len(top_levels) != 1:
        return None, "Zip must contain exactly one skill folder"
    folder = top_levels.pop()
    if f"{folder}/SKILL.md" not in zf.namelist():
        return None, "Skill folder must contain SKILL.md"
    return folder, None


@router.post("/api/import", dependencies=_INSTANCE_SKILLS)
async def import_skill(request: Request, file: UploadFile = File(...)) -> JSONResponse:
    from spark.skills.manager import SkillsManager, _parse_skill_dir

    manager = _manager(request)
    payload = await file.read()
    if len(payload) > _IMPORT_ZIP_MAX:
        return JSONResponse({"error": "Zip exceeds the 10MB import cap"}, status_code=400)
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        return JSONResponse({"error": "Not a valid zip file"}, status_code=400)

    folder, error = _validate_zip(zf)
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if manager.exists(folder) or (manager.user_dir / folder).exists():
        return JSONResponse({"error": f"A skill named '{folder}' already exists"}, status_code=400)

    staging = manager.user_dir / f".import-{uuid.uuid4().hex[:6]}"
    try:
        zf.extractall(staging)
        candidate = staging / folder
        record = _parse_skill_dir(candidate, "user")
        if not record["valid"]:
            return JSONResponse({"error": f"Invalid skill: {record['error']}"}, status_code=400)
        candidate.rename(manager.user_dir / folder)
        manager.reload()
        return JSONResponse({"status": "ok", "name": folder})
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@router.get("/api/download")
async def download_skill(request: Request, name: str) -> Any:
    manager = _manager(request)
    skill = manager.get_skill(name)
    if not skill:
        return JSONResponse({"error": "Unknown skill"}, status_code=404)
    root = manager.skill_path(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, f"{name}/{path.relative_to(root).as_posix()}")
    buf.seek(0)
    from spark.identity import audit

    await audit.emit_request(
        request,
        audit.EXPORT_DOWNLOADED,
        resource_type="skill",
        resource_id=name,
        details={"kind": "skill", "source": skill.get("source")},
    )
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )


@router.post("/api/delete", dependencies=_INSTANCE_SKILLS)
async def delete_skill(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await request.json()
    name = str(data.get("name", "")).strip()
    skill = manager.get_skill(name)
    if not skill:
        return JSONResponse({"error": "Unknown skill"}, status_code=404)
    if skill["source"] != "user":
        return JSONResponse({"error": "Bundled skills cannot be deleted"}, status_code=400)
    shutil.rmtree(manager.skill_path(name))
    manager.reload()
    return JSONResponse({"status": "ok"})


@router.post("/api/reload", dependencies=_INSTANCE_SKILLS)
async def reload_skills(request: Request) -> JSONResponse:
    _manager(request).reload()
    return JSONResponse({"status": "ok"})
