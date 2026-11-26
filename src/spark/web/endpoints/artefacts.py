"""The artefacts of a conversation, for the right-hand panel (PC3, PC5).

* ``GET /artefacts/api/{conversation_id}`` lists what the panel shows, in three groups:

  - ``generated``: documents SPARK created, documents fetched for the preview, charts
    and diagrams (the files of the conversation that nobody attached);
  - ``provided``: files attached in the conversation (in a shared conversation each
    names who attached it);
  - ``project``: the resources the conversation inherits from its project, grouped by
    level (only in a project conversation, and only for someone who may reach the
    project).

  Each item has its title, file name, type, size, time, the message that produced or
  attached it (``message_id``, null when none names it) and the kind the preview pane
  draws it with. The list is decided by the conversation's access decision
  (:func:`spark.web.scope.conversation_access`): every member sees the same list, and a
  stranger or a removed member is refused (404).

* ``PATCH /artefacts/api/{file_id}`` renames a file of a conversation, for its owner or
  a Contributor (Read-only 403, a stranger 404). The title is checked and neutralised
  (:mod:`spark.files.titles`), the rename is audited (``spark.artefact.renamed``, with
  no title in the details) and every viewer of the conversation is told
  (``artefacts_changed``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spark.database import conversation_members
from spark.database import files as files_db
from spark.database import projects as projects_db
from spark.database.scope import Capability, NotOwned, NotPermitted, allows
from spark.db.engine import Row
from spark.files.titles import TitleRefused
from spark.identity import audit
from spark.preview.kinds import classify
from spark.web.db import database
from spark.web.scope import ConversationAccess, conversation_access, forbidden, not_found

__all__ = ["router"]

router = APIRouter(prefix="/artefacts")

_NO_STORE = {"Cache-Control": "private, no-store"}


def _preview_kind(row: Row) -> str:
    kind, _ = classify(row["mime_type"], str(row["filename"]))
    return str(kind.value)


def _item(
    row: Row,
    *,
    title: str,
    kind: str,
    when: Any,
    message_id: int | None,
    can_rename: bool,
    provided_by: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "file_id": int(row["file_id"] if "file_id" in row else row["id"]),
        "title": title,
        "name": str(row["filename"]),
        "type": row["mime_type"] or "",
        "kind": kind,
        "preview_kind": _preview_kind(row),
        "size": int(row["size_bytes"]),
        "created_at": when,
        "message_id": message_id,
        "can_rename": can_rename,
        "provided_by": provided_by,
    }


async def _who(request: Request, guids: set[str], me: str) -> dict[str, dict[str, Any]]:
    from spark.web.endpoints.conversations import display_names

    names = await display_names(request, guids)
    return {
        guid: {
            "guid": guid,
            "name": info["name"],
            "initials": info["initials"],
            "is_me": guid == me,
        }
        for guid, info in names.items()
    }


@router.get("/api/{conversation_id}")
async def list_artefacts(request: Request, conversation_id: int) -> JSONResponse:
    """The conversation's artefacts in their three groups, for a member."""
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    db = database(request)
    me = access.user_guid
    may_rename = allows(access.role, Capability.CONTRIBUTE)
    rows = await files_db.list_conversation_artefacts(db, me, conversation_id)
    origins = await files_db.artefact_origins(db, me, conversation_id, [r["id"] for r in rows])
    shared = await conversation_members.is_shared(db, me, conversation_id)
    people = await _who(request, {str(r["user_guid"]) for r in rows} if shared else set(), me)

    generated: list[dict[str, Any]] = []
    provided: list[dict[str, Any]] = []
    for row in rows:
        is_provided = row["kind"] == "attachment"
        item = _item(
            row,
            title=str(row["title"]),
            kind=str(row["kind"]),
            when=row["created_at"],
            message_id=origins.get(int(row["id"])),
            can_rename=may_rename,
            provided_by=people.get(str(row["user_guid"])) if is_provided and shared else None,
        )
        (provided if is_provided else generated).append(item)

    project: list[dict[str, Any]] = []
    try:
        levels = await projects_db.conversation_resources(db, me, conversation_id)
    except NotOwned:
        levels = []
    titles = await files_db.artefact_titles(
        db, [r["file_id"] for level in levels for r in level.resources]
    )
    for level in levels:
        if not level.resources:
            continue
        items = []
        for resource in level.resources:
            title, kind = titles.get(int(resource["file_id"]), (str(resource["filename"]), ""))
            items.append(
                _item(
                    resource,
                    title=title,
                    kind=kind,
                    when=resource["added_at"],
                    message_id=None,
                    can_rename=False,
                )
            )
        project.append(
            {
                "project_id": level.project_id,
                "name": level.name,
                "level": level.level,
                "items": items,
            }
        )

    body = {
        "conversation_id": conversation_id,
        "shared": shared,
        "can_rename": may_rename,
        "generated": generated,
        "provided": provided,
        "project": project,
        "counts": {
            "generated": len(generated),
            "provided": len(provided),
            "project": sum(len(g["items"]) for g in project),
        },
    }
    return JSONResponse(body, headers=_NO_STORE)


@router.patch("/api/{file_id}")
async def rename_artefact(request: Request, file_id: int) -> JSONResponse:
    """Rename a file of a conversation (its owner or a Contributor)."""
    db = database(request)
    row = await files_db.get_readable_file(db, _user(request), file_id)
    if row is None or row["conversation_id"] is None:
        return not_found("File not found.")
    conversation_id = int(row["conversation_id"])
    access = await conversation_access(request, conversation_id, Capability.CONTRIBUTE)
    if not isinstance(access, ConversationAccess):
        return access
    try:
        body = await request.json()
    except ValueError:
        body = None
    title = body.get("title") if isinstance(body, dict) else None
    try:
        updated = await files_db.set_conversation_file_title(
            db, access.user_guid, conversation_id, file_id, title  # type: ignore[arg-type]
        )
    except TitleRefused as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except NotOwned:
        return not_found("File not found.")
    except NotPermitted:
        return forbidden()
    if updated is None:
        return not_found("File not found.")
    await audit.emit_request(
        request,
        audit.ARTEFACT_RENAMED,
        resource_type="file",
        resource_id=str(file_id),
        details={"conversation_id": conversation_id, "kind": str(updated["kind"])},
    )
    from spark.web.live import bus_for

    await bus_for(request.app).publish(conversation_id, "artefacts_changed", {"file_id": file_id})
    return JSONResponse({"file_id": file_id, "title": str(updated["title"])}, headers=_NO_STORE)


def _user(request: Request) -> str:
    from spark.identity.access import current_user_guid

    return current_user_guid(request)
