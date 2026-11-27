"""Projects: the JSON API (PC6 to PC15).

Every route answers for the signed-in user and decides access through
:mod:`spark.projects.access` (projects) and :func:`spark.web.scope.conversation_access`
(conversations): a project or conversation the user may not reach answers 404, a role
that does not allow what was asked 403 with the reason, a refusal of the rules 400 with
its message. State-changing routes are POST, PATCH and DELETE, so the CSRF token and the
Origin check apply to every one (:mod:`spark.web.security`).

Sharing, members and transfer are server mode only (404 in personal mode); everything
else works in both modes, resources in the personal file area there. Each change is
audited (``spark.project.*``) in server mode, and changes that take a conversation away
from someone close their open views (:mod:`spark.web.live`).

Routes (all under ``/projects/api``):

* ``GET ""``: the user's projects as trees (``owned``, ``shared``), with the conversations
  each person can see; ``POST ""``: create (``name``, ``description``, ``instructions``,
  ``parent_id`` for a sub-project).
* ``GET /{id}``: one project with its path, sub-projects, conversations, resources and
  (for the owner) members; ``PATCH /{id}``: name, description, instructions;
  ``POST /{id}/move``: a sub-project under another project of its tree (``parent_id``).
* ``GET /{id}/resources``, ``POST /{id}/resources`` (multipart ``file``),
  ``GET /{id}/resources/{file_id}`` (download), ``DELETE /{id}/resources/{file_id}``.
* ``GET /{id}/members``, ``POST /{id}/members`` (``user_id`` or ``user_guid``, ``role``,
  ``scope``), ``DELETE /{id}/members/{guid}`` (the owner removes; a member leaves).
* ``GET /{id}/delete-preview``, ``POST /{id}/delete`` (``mode``: ``keep`` or
  ``everything``), ``POST /{id}/transfer`` (``user_id`` or ``user_guid``).
* ``POST /conversations/{conversation_id}/move`` (``project_id`` or null, ``confirm``):
  409 with the report when the move needs confirming;
  ``GET /conversations/{conversation_id}/context``: the conversation's project path and
  inherited resources by level.
* ``GET /notices``, ``POST /notices/seen``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from spark import files as files_pkg
from spark.database import files as files_db
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.database import user_identities
from spark.database.projects import DeleteMode, TransferQuota
from spark.database.scope import Capability, NotOwned, NotPermitted
from spark.db.engine import Row
from spark.files.area import FileAreaNotConfigured
from spark.files.paths import PathRefused
from spark.files.store import UploadTooLarge
from spark.identity import audit
from spark.identity.access import current_user_guid
from spark.projects import notices
from spark.projects.access import (
    ProjectAccess,
    ProjectNeed,
    ProjectRefused,
    ProjectRole,
    ShareScope,
    require_project,
)
from spark.web.db import database, sync_database
from spark.web.scope import ConversationAccess, conversation_access, is_server, not_found

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects")

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()+-]")
_MULTIPART_OVERHEAD = 64 * 1024
_CHUNK = 256 * 1024
_NO_STORE = {"Cache-Control": "no-store"}
_PROJECT_NOT_FOUND = "Project not found"
_ROLE_REFUSAL = "Your role in this project does not allow this."


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _refused(exc: Exception) -> JSONResponse:
    """The answer for a refusal from the repositories: 403 for a role that does not allow
    it (the access decision's own wording is replaced), 404 for a project the user may not
    reach, 507 for a transfer over quota, 400 with the reason otherwise."""
    if isinstance(exc, NotPermitted):
        text = str(exc)
        return _error(403, _ROLE_REFUSAL if " may not " in text else text)
    if isinstance(exc, NotOwned):
        return not_found(_PROJECT_NOT_FOUND)
    if isinstance(exc, TransferQuota):
        return _error(507, str(exc))
    return _error(400, str(exc))


async def _access(
    request: Request, project_id: int, need: ProjectNeed
) -> ProjectAccess | JSONResponse:
    try:
        return await require_project(
            database(request), current_user_guid(request), project_id, need
        )
    except NotOwned as exc:
        return _refused(exc)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


async def _audit(request: Request, action: str, project_id: int, **details: Any) -> None:
    await audit.emit_request(
        request, action, resource_type="project", resource_id=str(project_id), details=details
    )


async def _announce(
    request: Request, conversation_ids: Sequence[int], event: str, data: dict[str, Any]
) -> None:
    from spark.web.live import bus_for

    bus = bus_for(request.app)
    for cid in conversation_ids:
        await bus.publish(int(cid), event, data)


async def _names(request: Request, guids: Sequence[str]) -> dict[str, dict[str, str]]:
    from spark.web.endpoints.conversations import display_names

    return await display_names(request, guids)


def _project_view(access: ProjectAccess) -> dict[str, Any]:
    p = access.project
    return {
        "id": access.id,
        "name": p["name"],
        "description": p["description"],
        "instructions": p["instructions"],
        "parent_id": access.parent_id,
        "root_id": access.root_id,
        "depth": access.depth,
        "owner_guid": access.owner_guid,
        "role": access.role.value,
        "scope": access.scope.value if access.scope is not None else None,
        "can_contribute": access.allows(ProjectNeed.CONTRIBUTE),
        "can_manage": access.allows(ProjectNeed.MANAGE),
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
    }


def _conversation_view(row: Row, me: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "project_id": row["project_id"],
        "owner_guid": row["owner_guid"],
        "is_mine": row["owner_guid"] == me,
        "conversation_type": row["conversation_type"],
        "last_updated": row["last_updated"],
    }


def _resource_view(row: Row) -> dict[str, Any]:
    return {
        "file_id": row["file_id"],
        "project_id": row["project_id"],
        "filename": row["filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "added_by": row["added_by"],
        "added_at": row["added_at"],
    }


# -- Projects ------------------------------------------------------------------------------


@router.get("/api")
async def list_projects(request: Request) -> JSONResponse:
    """The user's projects as trees: ``owned`` and ``shared`` (with me), each node with its
    sub-projects (``children``) and the conversations the user can see in it."""
    me = current_user_guid(request)
    db = database(request)
    rows = await projects_db.list_projects(db, me)
    talks = await projects_db.visible_conversations(db, me)
    nodes: dict[int, dict[str, Any]] = {}
    for row in rows:
        nodes[int(row["id"])] = {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "parent_id": row["parent_id"],
            "root_id": row["root_id"],
            "depth": row["depth"],
            "owner_guid": row["owner_guid"],
            "role": row["role"],
            "scope": row["scope"],
            "updated_at": row["updated_at"],
            "children": [],
            "conversations": [],
        }
    for talk in talks:
        node = nodes.get(int(talk["project_id"]))
        if node is not None:
            node["conversations"].append(_conversation_view(talk, me))
    owned, shared = [], []
    for node in nodes.values():
        parent = node["parent_id"]
        if parent is not None and int(parent) in nodes:
            nodes[int(parent)]["children"].append(node)
        elif node["role"] == ProjectRole.OWNER.value:
            owned.append(node)
        else:
            shared.append(node)
    owners = await _names(request, [str(n["owner_guid"]) for n in shared])
    for node in shared:
        node["owner_name"] = owners.get(str(node["owner_guid"]), {}).get("name", "")
    unseen = len(await projects_db.notices(db, me)) if is_server() else 0
    return JSONResponse(
        {"owned": owned, "shared": shared, "unseen_notices": unseen}, headers=_NO_STORE
    )


@router.post("/api")
async def create_project(request: Request) -> JSONResponse:
    """Create a project, or a sub-project (``parent_id``, the owner's alone)."""
    data = await request.json()
    me = current_user_guid(request)
    parent = data.get("parent_id")
    parent_id = None if parent in (None, "") else _int(parent)
    if parent not in (None, "") and parent_id is None:
        return _error(400, "parent_id must be a project id.")
    db = database(request)
    try:
        new = await projects_db.create_project(
            db,
            me,
            str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            instructions=str(data.get("instructions") or ""),
            parent_id=parent_id,
        )
        access = await require_project(db, me, new, ProjectNeed.VIEW)
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    await _audit(request, audit.PROJECT_CREATED, new, parent_id=parent_id, depth=access.depth)
    return JSONResponse({"project": _project_view(access)}, status_code=201)


# -- Notices -------------------------------------------------------------------------------


@router.get("/api/notices")
async def list_notices(request: Request) -> JSONResponse:
    """The signed-in user's unseen project notices, newest first."""
    rows = await projects_db.notices(database(request), current_user_guid(request))
    return JSONResponse({"notices": [dict(r) for r in rows]}, headers=_NO_STORE)


@router.post("/api/notices/seen")
async def mark_notices_seen(request: Request) -> JSONResponse:
    """Mark every notice of the signed-in user seen."""
    count = await projects_db.mark_notices_seen(database(request), current_user_guid(request))
    return JSONResponse({"seen": count})


@router.get("/api/{project_id}")
async def get_project(request: Request, project_id: int) -> JSONResponse:
    """One project: itself, its path from the top level, its sub-projects, the
    conversations the user can see in it, its resources, and members for the owner."""
    access = await _access(request, project_id, ProjectNeed.VIEW)
    if not isinstance(access, ProjectAccess):
        return access
    me = access.user_guid
    db = database(request)
    path = await projects_db.path_of(db, project_id)
    children = await projects_db.sub_projects(db, me, project_id)
    talks = await projects_db.visible_conversations(db, me, [project_id])
    resources = await projects_db.list_resources(db, me, project_id)
    body: dict[str, Any] = {
        "project": _project_view(access),
        "path": [{"id": p["id"], "name": p["name"], "depth": p["depth"]} for p in path],
        "sub_projects": [dict(c) for c in children],
        "conversations": [_conversation_view(t, me) for t in talks],
        "resources": [_resource_view(r) for r in resources],
        "members": None,
    }
    if access.allows(ProjectNeed.MANAGE) and is_server():
        body["members"] = await _members_view(request, access)
    return JSONResponse(body, headers=_NO_STORE)


@router.patch("/api/{project_id}")
async def update_project(request: Request, project_id: int) -> JSONResponse:
    """Change the name, description or instructions (the owner's alone)."""
    data = await request.json()
    me = current_user_guid(request)

    def field(name: str) -> str | None:
        value = data.get(name)
        return None if value is None else str(value)

    try:
        changed = await projects_db.update_project(
            database(request),
            me,
            project_id,
            name=field("name"),
            description=field("description"),
            instructions=field("instructions"),
        )
        access = await require_project(database(request), me, project_id, ProjectNeed.VIEW)
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    if changed:
        await _audit(request, audit.PROJECT_UPDATED, project_id, fields=changed)
    return JSONResponse({"project": _project_view(access), "changed": changed})


@router.post("/api/{project_id}/move")
async def move_project(request: Request, project_id: int) -> JSONResponse:
    """Move a sub-project under another project of its tree (``parent_id``)."""
    data = await request.json()
    parent_id = _int(data.get("parent_id"))
    if parent_id is None:
        return _error(400, "parent_id must be a project id.")
    try:
        old, new = await projects_db.move_project(
            database(request), current_user_guid(request), project_id, parent_id
        )
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    await _audit(request, audit.PROJECT_MOVED, project_id, from_parent=old, to_parent=new)
    return JSONResponse({"status": "moved", "parent_id": new})


# -- Resources -----------------------------------------------------------------------------


def _safe_name(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = _UNSAFE.sub("_", base).strip(" .")[:120].strip(" .")
    return cleaned or "upload"


@router.get("/api/{project_id}/resources")
async def list_resources(request: Request, project_id: int) -> JSONResponse:
    """The resources of one project (not those it inherits)."""
    try:
        rows = await projects_db.list_resources(
            database(request), current_user_guid(request), project_id
        )
    except NotOwned as exc:
        return _refused(exc)
    return JSONResponse({"resources": [_resource_view(r) for r in rows]}, headers=_NO_STORE)


@router.post("/api/{project_id}/resources")
async def upload_resource(request: Request, project_id: int) -> Response:
    """Upload a resource into the project owner's file area, charged to the owner's quota
    under the upload limit, whoever uploads it (owner or Contributor; PC11)."""
    access = await _access(request, project_id, ProjectNeed.CONTRIBUTE)
    if not isinstance(access, ProjectAccess):
        return access
    try:
        area = files_pkg.file_area(sync_database(request))
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
    filename = _safe_name(upload.filename)
    owner = access.owner_guid
    key = f"projects/{access.root_id}/{uuid.uuid4().hex}/{filename}"
    try:
        file_id = await asyncio.to_thread(
            area.save,
            owner,
            key,
            upload.file,
            filename=filename,
            mime_type=upload.content_type,
            source="project",
        )
    except UploadTooLarge as exc:
        return _error(413, str(exc))
    except files_db.QuotaExceeded as exc:
        return _error(507, f"The project owner's storage is full. {exc}")
    except PathRefused as exc:
        return _error(400, str(exc))
    finally:
        await upload.close()
    db = database(request)
    try:
        await projects_db.add_resource(db, access.user_guid, project_id, file_id)
    except (NotOwned, ProjectRefused) as exc:
        # Access changed during the upload: nothing is left behind.
        await files_db.delete_user_file(db, owner, file_id)
        await asyncio.to_thread(area.store.delete, owner, key)
        return _refused(exc)
    row = await projects_db.project_resource(db, access.user_guid, project_id, file_id)
    assert row is not None
    await _audit(
        request,
        audit.PROJECT_RESOURCE_ADDED,
        project_id,
        file_id=file_id,
        size_bytes=row["size_bytes"],
    )
    return JSONResponse({"resource": _resource_view(row)}, status_code=201)


def _iterate(handle: Any) -> Iterator[bytes]:
    with handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _disposition(filename: str) -> str:
    from urllib.parse import quote

    fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/api/{project_id}/resources/{file_id}")
async def download_resource(request: Request, project_id: int, file_id: int) -> Response:
    """Download a resource through the project's access check (anyone who may view it)."""
    row = await projects_db.project_resource(
        database(request), current_user_guid(request), project_id, file_id
    )
    if row is None:
        return not_found("File not found.")
    try:
        store = files_pkg.file_store()
        handle = await asyncio.to_thread(
            store.open, str(row["owner_guid"]), str(row["storage_key"])
        )
    except FileAreaNotConfigured as exc:
        return _error(503, f"File storage is not available: {exc}.")
    except (FileNotFoundError, PathRefused):
        return not_found("File not found.")
    await audit.emit_request(
        request,
        audit.FILE_DOWNLOADED,
        resource_type="file",
        resource_id=str(file_id),
        details={"size_bytes": row["size_bytes"], "project_id": project_id},
    )
    return StreamingResponse(
        _iterate(handle),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _disposition(str(row["filename"])),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/api/{project_id}/resources/{file_id}")
async def remove_resource(request: Request, project_id: int, file_id: int) -> JSONResponse:
    """Remove a resource: the owner any, a Contributor those they added. A file uploaded as
    a resource is deleted from the owner's area and its size released."""
    db = database(request)
    try:
        gone = await projects_db.remove_resource(
            db, current_user_guid(request), project_id, file_id
        )
    except (NotPermitted, ProjectRefused) as exc:
        return _refused(exc)
    except NotOwned:
        return not_found("Resource not found.")
    if gone is not None:
        await _remove_stored([gone])
    await _audit(request, audit.PROJECT_RESOURCE_REMOVED, project_id, file_id=file_id)
    return JSONResponse({"removed": file_id})


async def _remove_stored(rows: Sequence[Row]) -> None:
    """Remove deleted files' content from their owner's storage (their rows are gone)."""
    try:
        store = files_pkg.file_store()
    except FileAreaNotConfigured:
        return
    for row in rows:
        try:
            await asyncio.to_thread(store.delete, str(row["user_guid"]), str(row["storage_key"]))
        except (OSError, PathRefused):
            logger.warning("A deleted project resource's content could not be removed")


# -- Members (server mode) -----------------------------------------------------------------


async def _members_view(request: Request, access: ProjectAccess) -> list[dict[str, Any]]:
    rows = await members_db.members(database(request), access.user_guid, access.id)
    names = await _names(request, [str(r["user_guid"]) for r in rows])
    return [
        {
            "user_guid": r["user_guid"],
            "role": r["role"],
            "scope": r["scope"],
            "name": names[str(r["user_guid"])]["name"],
            "initials": names[str(r["user_guid"])]["initials"],
            "added_at": r["added_at"],
        }
        for r in rows
    ]


@router.get("/api/{project_id}/members")
async def list_members(request: Request, project_id: int) -> JSONResponse:
    """The people a top-level project is shared with (the owner only)."""
    if not is_server():
        return not_found()
    access = await _access(request, project_id, ProjectNeed.MANAGE)
    if not isinstance(access, ProjectAccess):
        return access
    return JSONResponse({"members": await _members_view(request, access)}, headers=_NO_STORE)


@router.get("/api/{project_id}/people")
async def list_people(request: Request, project_id: int) -> JSONResponse:
    """Who has a project's tree: its owner and every member, for anyone who may view it.

    The share picker limits a conversation in the project to these people (PC9). Each
    entry names the warden user id the people search returns, the display name and the
    initials; no role, scope or email. A sub-project answers for its top-level project.
    """
    if not is_server():
        return not_found()
    access = await _access(request, project_id, ProjectNeed.VIEW)
    if not isinstance(access, ProjectAccess):
        return access
    db = database(request)
    guids = await members_db.people(db, access.user_guid, access.id)
    names = await _names(request, guids)
    found: list[dict[str, str]] = []
    for guid in guids:
        warden_id = await user_identities.warden_user_for(db, guid)
        if warden_id is None:
            continue
        found.append(
            {
                "id": warden_id,
                "name": names[guid]["name"],
                "initials": names[guid]["initials"],
            }
        )
    return JSONResponse({"people": found}, headers=_NO_STORE)


def _role_of(value: Any) -> ProjectRole | None:
    try:
        role = ProjectRole(str(value))
    except ValueError:
        return None
    return role if role in (ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER) else None


def _scope_of(value: Any) -> ShareScope | None:
    try:
        return ShareScope(str(value))
    except ValueError:
        return None


def _share_detail(role: ProjectRole, scope: ShareScope) -> str:
    who = "as a Contributor" if role is ProjectRole.CONTRIBUTOR else "read-only"
    what = (
        "with all its conversations"
        if scope is ShareScope.ALL_CONVERSATIONS
        else "without its conversations"
    )
    return f"{who}, {what}"


async def _person(request: Request, data: dict[str, Any]) -> tuple[str, Any] | JSONResponse:
    """The SPARK guid (and warden user) a request names by ``user_id`` (an active warden
    user) or ``user_guid`` (someone already known)."""
    from spark.web.endpoints.conversations import _active_user, _warden_user

    db = database(request)
    if data.get("user_id"):
        user = await _active_user(request, str(data["user_id"]))
        if user is None:
            return _error(400, "No active user found")
        return await user_identities.user_guid_for(db, str(user.id)), user
    guid = str(data.get("user_guid") or "")
    if not guid:
        return _error(400, "Name a person: user_id or user_guid.")
    member = await _warden_user(request, guid)
    user = await _active_user(request, str(member.id)) if member is not None else None
    return guid, user


def _link(request: Request, path: str) -> str:
    runtime = getattr(request.app.state, "identity_runtime", None)
    base = str(runtime.config.http.public_base_url).rstrip("/") if runtime is not None else ""
    return f"{base}{path}"


@router.post("/api/{project_id}/members")
async def share_project(request: Request, project_id: int) -> JSONResponse:
    """Share a top-level project with a person, or change their role or scope (the owner
    only). A narrowed share closes their views of the conversations they lose."""
    if not is_server():
        return not_found()
    access = await _access(request, project_id, ProjectNeed.MANAGE)
    if not isinstance(access, ProjectAccess):
        return access
    data = await request.json()
    role, scope = _role_of(data.get("role")), _scope_of(data.get("scope"))
    if role is None or scope is None:
        return _error(
            400, "role must be contributor or viewer, scope project_only or all_conversations"
        )
    found = await _person(request, data)
    if isinstance(found, JSONResponse):
        return found
    target, user = found
    if data.get("user_guid") and not data.get("user_id"):
        known = {m["user_guid"] for m in await _members_view(request, access)}
        if target not in known:
            return _error(400, "Not a member")
    try:
        change = await members_db.share(
            database(request), access.user_guid, project_id, target, role, scope
        )
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    if change.outcome == "added":
        await _audit(
            request,
            audit.PROJECT_MEMBER_ADDED,
            project_id,
            member=target,
            role=role.value,
            scope=scope.value,
        )
        if user is not None:
            sharer = await _names(request, [access.user_guid])
            await notices.send_project_notice(
                request.app.state.ctx.secrets,
                user.email,
                kind="shared",
                actor=sharer[access.user_guid]["name"],
                project=str(access.project["name"]),
                detail=_share_detail(role, scope),
                link=_link(request, "/"),
            )
    elif change.outcome == "changed":
        await _audit(
            request,
            audit.PROJECT_MEMBER_CHANGED,
            project_id,
            member=target,
            role=role.value,
            scope=scope.value,
            previous_role=change.previous_role.value if change.previous_role else None,
            previous_scope=change.previous_scope.value if change.previous_scope else None,
        )
    await _announce(request, change.lost, "member_removed", {"user_guid": target})
    return JSONResponse({"status": change.outcome, "members": await _members_view(request, access)})


@router.delete("/api/{project_id}/members/{member_guid}")
async def remove_member(request: Request, project_id: int, member_guid: str) -> JSONResponse:
    """The owner removes a person, or a member leaves (their own guid). Their views of the
    conversations they lose close at once."""
    if not is_server():
        return not_found()
    me = current_user_guid(request)
    db = database(request)
    try:
        if member_guid == me:
            change = await members_db.leave(db, me, project_id)
        else:
            change = await members_db.remove(db, me, project_id, member_guid)
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    if change.outcome != "removed":
        return not_found("Not a member")
    await _audit(
        request,
        audit.PROJECT_MEMBER_REMOVED,
        project_id,
        member=member_guid,
        role=change.previous_role.value if change.previous_role else None,
        left=member_guid == me,
    )
    await _announce(request, change.lost, "member_removed", {"user_guid": member_guid})
    await _announce(request, change.lost, "members_changed", {})
    return JSONResponse({"status": "removed"})


# -- Conversations in projects -------------------------------------------------------------


@router.post("/api/conversations/{conversation_id}/move")
async def move_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """Move a conversation into a project, out to no project (``project_id`` null) or
    between levels of its project. Answers 409 with the report, moving nothing, when the
    move would show it to more people or hide it from some and ``confirm`` is not true."""
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    data = await request.json()
    raw = data.get("project_id")
    target = None if raw in (None, "") else _int(raw)
    if raw not in (None, "") and target is None:
        return _error(400, "project_id must be a project id or null.")
    try:
        report = await projects_db.move_conversation(
            database(request),
            access.user_guid,
            conversation_id,
            target,
            confirm=data.get("confirm") is True,
        )
    except NotPermitted as exc:
        return _error(403, str(exc))
    except NotOwned:
        return not_found(_PROJECT_NOT_FOUND)
    except ProjectRefused as exc:
        return _error(400, str(exc))
    names = await _names(
        request, list(report.widened) + list(report.narrowed) + list(report.shares_removed)
    )
    body = {
        **report.summary(),
        "names": {guid: value["name"] for guid, value in names.items()},
    }
    if report.confirm_required:
        return JSONResponse(
            {
                **body,
                "error": "This move changes who can see the conversation; confirm it to go on.",
            },
            status_code=409,
        )
    await _audit(
        request,
        audit.PROJECT_CONVERSATION_MOVED,
        report.to_project or report.from_project or 0,
        conversation_id=conversation_id,
        from_project=report.from_project,
        to_project=report.to_project,
        widened=len(report.widened),
        narrowed=len(report.narrowed),
    )
    for guid, now in report.narrowed.items():
        if now is None:
            await _announce(request, [conversation_id], "member_removed", {"user_guid": guid})
    await _announce(request, [conversation_id], "members_changed", {})
    return JSONResponse(body)


@router.get("/api/conversations/{conversation_id}/context")
async def conversation_context(request: Request, conversation_id: int) -> JSONResponse:
    """A conversation's project path (breadcrumbs) and the resources it inherits, by level
    (the right panel's Project resources group). Empty outside a project."""
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    try:
        levels = await projects_db.conversation_resources(
            database(request), access.user_guid, conversation_id
        )
    except NotOwned:
        return not_found()
    return JSONResponse(
        {
            "conversation_id": conversation_id,
            "project_id": levels[-1].project_id if levels else None,
            "path": [{"id": lv.project_id, "name": lv.name, "depth": lv.level} for lv in levels],
            "levels": [
                {
                    "project_id": lv.project_id,
                    "name": lv.name,
                    "level": lv.level,
                    "resources": [_resource_view(r) for r in lv.resources],
                }
                for lv in levels
            ],
        },
        headers=_NO_STORE,
    )


# -- Deletion and transfer -----------------------------------------------------------------


@router.get("/api/{project_id}/delete-preview")
async def delete_preview(request: Request, project_id: int) -> JSONResponse:
    """What deleting the project affects: the counts for the confirmation (the owner's)."""
    try:
        preview = await projects_db.delete_preview(
            database(request), current_user_guid(request), project_id
        )
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    return JSONResponse(preview.summary(), headers=_NO_STORE)


@router.post("/api/{project_id}/delete")
async def delete_project(request: Request, project_id: int) -> JSONResponse:
    """Delete a project with its sub-projects and resources: ``mode`` ``keep`` keeps its
    conversations, ``everything`` deletes them too, and tells their starters."""
    data = await request.json()
    try:
        mode = DeleteMode(str(data.get("mode")))
    except ValueError:
        return _error(400, "mode must be keep or everything")
    me = current_user_guid(request)
    try:
        result = await projects_db.delete_project(database(request), me, project_id, mode)
    except (NotOwned, ProjectRefused) as exc:
        return _refused(exc)
    await _remove_stored(result.files)
    await _audit(
        request,
        audit.PROJECT_DELETED,
        project_id,
        mode=mode.value,
        sub_projects=result.sub_projects,
        resources=result.resources,
        conversations_kept=len(result.conversations_kept),
        conversations_deleted=len(result.conversations_deleted),
    )
    await _announce(request, result.conversations_deleted, "conversation_deleted", {})
    await _announce(request, result.conversations_kept, "members_changed", {})
    if result.told and is_server():
        from spark.web.endpoints.conversations import _warden_user

        actor = (await _names(request, [me]))[me]["name"]
        for guid, count in result.told.items():
            person = await _warden_user(request, guid)
            if person is not None:
                await notices.send_project_notice(
                    request.app.state.ctx.secrets,
                    person.email,
                    kind="deleted",
                    actor=actor,
                    project=result.name,
                    count=count,
                    link=_link(request, "/"),
                )
    return JSONResponse(
        {
            "status": "deleted",
            "mode": mode.value,
            "conversations_kept": len(result.conversations_kept),
            "conversations_deleted": len(result.conversations_deleted),
            "people_told": len(result.told),
        }
    )


@router.post("/api/{project_id}/transfer")
async def transfer_project(request: Request, project_id: int) -> JSONResponse:
    """Make a member (``user_guid``) or a person added in the same step (``user_id``) the
    owner of a top-level project. Their storage must hold its resources (507 otherwise)."""
    if not is_server():
        return not_found()
    access = await _access(request, project_id, ProjectNeed.MANAGE)
    if not isinstance(access, ProjectAccess):
        return access
    data = await request.json()
    found = await _person(request, data)
    if isinstance(found, JSONResponse):
        return found
    target, user = found
    if user is None:
        return _error(400, "No active user found")
    try:
        store = files_pkg.file_store()
        quota = files_pkg.file_limits().quota_bytes
    except FileAreaNotConfigured:
        store, quota = None, None
    written: list[str] = []

    def copy_one(key: str) -> None:
        assert store is not None
        with store.open(access.owner_guid, key) as source:
            store.put(target, key, source)
        written.append(key)

    async def copy(files: Sequence[Row]) -> None:
        if store is None:
            raise ProjectRefused("File storage is not available to move the resources.")
        for row in files:
            await asyncio.to_thread(copy_one, str(row["storage_key"]))

    try:
        result = await projects_db.transfer_project(
            database(request),
            access.user_guid,
            project_id,
            target,
            quota_bytes=quota,
            add_member=bool(data.get("user_id")),
            copy_files=copy,
        )
    except (NotOwned, ProjectRefused) as exc:
        for key in written:
            if store is not None:
                await asyncio.to_thread(store.delete, target, key)
        return _refused(exc)
    except OSError:
        for key in written:
            if store is not None:
                await asyncio.to_thread(store.delete, target, key)
        return _error(503, "The project's resources could not be moved; nothing changed.")
    if store is not None:
        for row in result.files:
            try:
                await asyncio.to_thread(store.delete, access.owner_guid, str(row["storage_key"]))
            except (OSError, PathRefused):
                logger.warning("A transferred resource's old copy could not be removed")
    await _audit(
        request,
        audit.PROJECT_TRANSFERRED,
        project_id,
        new_owner=target,
        files=len(result.files),
        added_member=result.added_member,
    )
    sharer = await _names(request, [access.user_guid])
    await notices.send_project_notice(
        request.app.state.ctx.secrets,
        user.email,
        kind="transferred",
        actor=sharer[access.user_guid]["name"],
        project=result.name,
        link=_link(request, "/"),
    )
    return JSONResponse({"status": "transferred", "files": len(result.files)})
