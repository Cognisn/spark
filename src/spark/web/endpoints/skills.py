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

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills")

_IMPORT_ZIP_MAX = 10 * 1024 * 1024


def _manager(request: Request) -> Any:
    m = getattr(request.app.state, "skills_manager", None)
    if m is not None:
        return m
    from spark.skills.manager import get_skills_manager

    return get_skills_manager()


def _db(request: Request) -> Any:
    return request.app.state.conversation_manager._db


def _user_guid(request: Request) -> str:
    return getattr(request.app.state, "user_guid", "default")


@router.get("", response_class=HTMLResponse)
async def skills_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "skills.html")


@router.get("/api/list")
async def list_skills(request: Request) -> JSONResponse:
    """All discovered skills (including invalid), joined with enable state."""
    from spark.database import skills as skills_db

    manager = _manager(request)
    states = skills_db.get_skill_states(_db(request), _user_guid(request))
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
            }
        )
    return JSONResponse({"skills": out})


@router.post("/api/toggle")
async def toggle_skill(request: Request) -> JSONResponse:
    from spark.database import skills as skills_db

    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    skills_db.set_skill_enabled(_db(request), name, bool(data.get("enabled", True)), _user_guid(request))
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


@router.post("/api/import")
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
        return JSONResponse(
            {"error": f"A skill named '{folder}' already exists"}, status_code=400
        )

    staging = manager.user_dir / f".import-{uuid.uuid4().hex[:6]}"
    try:
        zf.extractall(staging)
        candidate = staging / folder
        record = _parse_skill_dir(candidate, "user")
        if not record["valid"]:
            return JSONResponse(
                {"error": f"Invalid skill: {record['error']}"}, status_code=400
            )
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
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )


@router.post("/api/delete")
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


@router.post("/api/reload")
async def reload_skills(request: Request) -> JSONResponse:
    _manager(request).reload()
    return JSONResponse({"status": "ok"})
