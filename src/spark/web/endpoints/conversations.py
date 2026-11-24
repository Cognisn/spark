"""Conversation management endpoints, and sharing (server mode).

The owner of a conversation shares it with other active users of the instance, as
contributor or viewer, changes their roles, removes them and transfers ownership;
anyone else may leave. Every route that takes a conversation id asks
:func:`spark.web.scope.conversation_access` for what it needs. Each share, role
change, removal and transfer is audited with the client address, and announced
live to everyone viewing the conversation (:mod:`spark.web.live`). Personal mode
has one user, so sharing answers 404 there and the page hides it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spark.database import conversation_members, conversations, user_identities
from spark.database.scope import Capability, NotOwned, NotPermitted, Role
from spark.identity.access import current_identity, current_user_guid
from spark.web.db import database
from spark.web.models import not_offered
from spark.web.scope import (
    ConversationAccess,
    conversation_access,
    forbidden,
    is_server,
    not_found,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations")


@router.get("", response_class=HTMLResponse)
async def conversations_page(request: Request) -> HTMLResponse:
    """Render the conversations list page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "conversations.html")


@router.get("/api/list")
async def list_conversations(request: Request) -> JSONResponse:
    """API: list all active conversations."""
    ctx = request.app.state.ctx
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])

    user_guid = current_user_guid(request)
    convs = await asyncio.to_thread(conv_mgr.get_conversations, user_guid)
    return JSONResponse(convs)


@router.get("/api/shared")
async def list_shared(request: Request) -> JSONResponse:
    """API: the conversations shared with the signed-in user, with the count not yet opened."""
    if not is_server():
        return JSONResponse({"conversations": [], "unseen": 0})
    user_guid = current_user_guid(request)
    db = database(request)
    rows = await conversation_members.shared_with(db, user_guid)
    names = await display_names(request, {str(r["owner_guid"]) for r in rows})
    listed = [
        {**r, "owner_name": names.get(str(r["owner_guid"]), {}).get("name", "")} for r in rows
    ]
    return JSONResponse(
        {"conversations": listed, "unseen": sum(1 for r in rows if r["is_new"])},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/create")
async def create_conversation(request: Request) -> JSONResponse:
    """API: create a new conversation."""
    data = await request.json()
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Conversation manager not initialised"}, status_code=503)

    name = data.get("name", "New Conversation")
    model_id = data.get("model_id", "")
    instructions = data.get("instructions")
    web_search = data.get("web_search_enabled", False)
    conversation_type = data.get("conversation_type", "standard")
    user_guid = current_user_guid(request)

    # Validate a debate payload BEFORE creating anything, so a bad request
    # never leaves an orphan conversation row.
    debate = None
    if conversation_type == "debate":
        debate = data.get("debate") or {}
        agents = debate.get("agents") or {}
        if not all(
            role in agents and (agents[role] or {}).get("model_id")
            for role in ("pro", "con", "judge")
        ):
            return JSONResponse(
                {"error": "Debate requires pro, con and judge agents with models"},
                status_code=400,
            )
        if not (debate.get("topic") or "").strip():
            return JSONResponse({"error": "Debate requires a topic"}, status_code=400)
        for role, spec in agents.items():
            for key in ("allowed_tools", "allowed_skills"):
                value = (spec or {}).get(key)
                if value is not None and (
                    not isinstance(value, list) or any(not isinstance(item, str) for item in value)
                ):
                    return JSONResponse(
                        {"error": f"{key} for {role} must be a list of tool/skill names"},
                        status_code=400,
                    )

    # Validate a panel payload BEFORE creating anything, for the same reason.
    panel = None
    panel_agents = None
    if conversation_type == "panel":
        panel = data.get("panel") or {}
        if not (panel.get("topic") or "").strip():
            return JSONResponse({"error": "Panel requires a topic"}, status_code=400)
        moderator = panel.get("moderator") or {}
        if not moderator.get("model_id"):
            return JSONResponse(
                {"error": "Panel requires a moderator with a model"}, status_code=400
            )
        panellists = panel.get("panellists") or []
        if not isinstance(panellists, list) or not 2 <= len(panellists) <= 5:
            return JSONResponse({"error": "Panel requires 2 to 5 panellists"}, status_code=400)
        human = panel.get("human") or None
        names = [(p or {}).get("name", "").strip() for p in panellists]
        if human is not None:
            human_name = (human.get("name") or "").strip()
            if not human_name:
                return JSONResponse({"error": "Human panellist requires a name"}, status_code=400)
            names.append(human_name)
        if any(not n for n in names) or len({n.casefold() for n in names}) != len(names):
            return JSONResponse(
                {"error": "Panellists need distinct, non-empty names"}, status_code=400
            )
        if any(not (p or {}).get("model_id") for p in panellists):
            return JSONResponse({"error": "Every AI panellist requires a model"}, status_code=400)
        specs = [("moderator", moderator)] + [
            (f"panellist:{i}", p) for i, p in enumerate(panellists, start=1)
        ]
        for role, spec in specs:
            for key in ("allowed_tools", "allowed_skills"):
                value = (spec or {}).get(key)
                if value is not None and (
                    not isinstance(value, list) or any(not isinstance(item, str) for item in value)
                ):
                    return JSONResponse(
                        {"error": f"{key} for {role} must be a list of tool/skill names"},
                        status_code=400,
                    )
        panel_agents = {
            "moderator": {
                "model_id": moderator["model_id"],
                "brief": moderator.get("brief"),
                "allowed_skills": moderator.get("allowed_skills"),
                "display_name": "Moderator",
                "voice_id": moderator.get("voice_id") or None,
            }
        }
        for i, spec in enumerate(panellists, start=1):
            panel_agents[f"panellist:{i}"] = {
                "model_id": spec["model_id"],
                "brief": spec.get("brief"),
                "allowed_tools": spec.get("allowed_tools"),
                "allowed_skills": spec.get("allowed_skills"),
                "display_name": spec["name"].strip(),
                "voice_id": spec.get("voice_id") or None,
            }
        if human is not None:
            panel_agents[f"panellist:{len(panellists) + 1}"] = {
                "model_id": "",
                "brief": None,
                "display_name": (human.get("name") or "").strip(),
                "is_human": True,
            }

    # Every model chosen here, the debate's and panel's agents included, must be offered.
    chosen: list[object] = [model_id]
    if debate is not None:
        chosen += [(spec or {}).get("model_id") for spec in (debate.get("agents") or {}).values()]
    if panel_agents is not None:
        chosen += [spec.get("model_id") for spec in panel_agents.values()]
    if refused := not_offered(request, *chosen):
        return refused

    try:
        cid = await asyncio.to_thread(
            conv_mgr.create_conversation,
            name,
            model_id,
            user_guid,
            instructions=instructions,
            web_search_enabled=web_search,
        )
        if conversation_type == "debate" and debate is not None:
            from spark.database import debates

            db = database(request)
            await conversations.update_conversation(db, cid, user_guid, conversation_type="debate")
            await debates.create_debate(
                db,
                cid,
                debate["topic"].strip(),
                debate.get("rounds_mode", "fixed"),
                debate.get("max_rounds"),
                user_guid,
                debate["agents"],
            )
        if conversation_type == "panel" and panel_agents is not None:
            from spark.database import debates

            db = database(request)
            await conversations.update_conversation(db, cid, user_guid, conversation_type="panel")
            await debates.create_debate(
                db,
                cid,
                panel["topic"].strip(),
                panel.get("rounds_mode", "fixed"),
                panel.get("max_rounds"),
                user_guid,
                panel_agents,
            )
        if data.get("kg_local_enabled"):
            await conversations.update_conversation(
                database(request), cid, user_guid, kg_local_enabled=1
            )
        return JSONResponse({"id": cid, "name": name, "conversation_type": conversation_type})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.delete("/api/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """API: delete a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    await asyncio.to_thread(conv_mgr.delete_conversation, conversation_id, access.user_guid)
    await _announce(request, conversation_id, "conversation_deleted", {})
    return JSONResponse({"status": "ok"})


@router.get("/api/search")
async def search_conversations(request: Request) -> JSONResponse:
    """API: search conversations by keyword."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])

    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse([])

    user_guid = current_user_guid(request)
    from spark.database import conversations

    results = await conversations.search_conversations(database(request), query, user_guid)
    return JSONResponse(results)


@router.post("/api/{conversation_id}/rename")
async def rename_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """API: rename a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)

    user_guid = current_user_guid(request)
    from spark.database import conversations

    await conversations.update_conversation(
        database(request), conversation_id, user_guid, name=name
    )
    return JSONResponse({"status": "ok"})


@router.post("/api/{conversation_id}/model")
async def change_model(request: Request, conversation_id: int) -> JSONResponse:
    """API: change the model for a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    data = await request.json()
    model_id = data.get("model_id", "").strip()
    if not model_id:
        return JSONResponse({"error": "model_id required"}, status_code=400)
    if refused := not_offered(request, model_id):
        return refused

    user_guid = current_user_guid(request)
    from spark.database import conversations

    await conversations.update_conversation(
        database(request), conversation_id, user_guid, model_id=model_id
    )
    return JSONResponse({"status": "ok"})


@router.post("/api/{conversation_id}/favourite")
async def toggle_favourite(request: Request, conversation_id: int) -> JSONResponse:
    """API: star or unstar a conversation, for the signed-in user alone (owner or member)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    data = await request.json()
    await conversation_members.set_favourite(
        database(request), access.user_guid, conversation_id, bool(data.get("favourite", False))
    )
    return JSONResponse({"status": "ok"})


@router.get("/api/models")
async def list_models(request: Request) -> JSONResponse:
    """API: list available models, respecting default_model settings."""
    from spark.web.models import connections, default_model, default_model_mode, listed

    if connections(request) is None:
        return JSONResponse({"models": [], "default_model": None, "mandatory": False})

    all_models = listed(request)
    # The signed-in user's default on a server (offered only); config.yaml's otherwise.
    default_model_id = await default_model(request, current_user_guid(request))
    mode = await default_model_mode(request)
    mandatory = mode == "mandatory" and bool(default_model_id)

    if mandatory:
        # Only return the mandatory model
        filtered = [m for m in all_models if m["id"] == default_model_id]
        if not filtered:
            # Model not found in available models — still return it so user sees something
            filtered = [
                {
                    "id": default_model_id,
                    "name": default_model_id,
                    "provider": "configured",
                }
            ]
        return JSONResponse(
            {"models": filtered, "default_model": default_model_id, "mandatory": True}
        )

    return JSONResponse(
        {
            "models": all_models,
            "default_model": default_model_id or None,
            "mandatory": False,
        }
    )


# -- People and names -----------------------------------------------------------------------

PEOPLE_MIN_QUERY = 2
PEOPLE_LIMIT = 10
_PAGE = 200


def _runtime(request: Request) -> Any:
    return getattr(request.app.state, "identity_runtime", None)


async def _warden_user(request: Request, user_guid: str) -> Any:
    """The warden user behind a SPARK guid, or None."""
    runtime = _runtime(request)
    if runtime is None:
        return None
    warden_id = await user_identities.warden_user_for(database(request), user_guid)
    if warden_id is None:
        return None
    try:
        return await runtime.identity.users.get(uuid.UUID(warden_id))
    except ValueError:
        return None


async def display_names(request: Request, guids: Iterable[str]) -> dict[str, dict[str, str]]:
    """``{guid: {"name", "initials"}}`` for the people named by ``guids``.

    The name is the user's display name, else their email address; a guid with no
    user (a person since deleted) is "Former member". Personal mode has one person.
    """
    found: dict[str, dict[str, str]] = {}
    for guid in {g for g in guids if g}:
        if not is_server():
            name = "You"
        else:
            user = await _warden_user(request, guid)
            name = (user.display_name or user.email) if user is not None else ""
            name = name or "Former member"
        found[guid] = {"name": name, "initials": initials(name)}
    return found


def initials(name: str) -> str:
    """Up to two capital letters for a name (an email's local part counts as the name)."""
    base = name.split("@", 1)[0]
    words = [w for w in base.replace(".", " ").replace("_", " ").split() if w[:1].isalnum()]
    letters = "".join(w[0] for w in words[:2]) or base[:1] or "?"
    return letters.upper()


@router.get("/api/people")
async def search_people(request: Request) -> JSONResponse:
    """API: active users of the instance whose name or email contains ``q`` (server mode).

    At least two characters, at most ten results, never the caller.
    """
    if not is_server():
        return not_found()
    query = request.query_params.get("q", "").strip().casefold()
    if len(query) < PEOPLE_MIN_QUERY:
        return JSONResponse([])
    runtime = _runtime(request)
    me = current_identity(request)
    if runtime is None or me is None:
        return not_found()
    from warden.identity import UserStatus

    found: list[dict[str, str]] = []
    after: str | None = None
    while len(found) < PEOPLE_LIMIT:
        page = await runtime.identity.users.list(status=UserStatus.ACTIVE, limit=_PAGE, after=after)
        for user in page:
            if str(user.id) == me.warden_user_id:
                continue
            name = user.display_name or ""
            if query in name.casefold() or query in user.email.casefold():
                found.append({"id": str(user.id), "name": name or user.email, "email": user.email})
                if len(found) == PEOPLE_LIMIT:
                    break
        if len(page) < _PAGE:
            break
        after = runtime.identity.users.cursor(page[-1])
    return JSONResponse(found, headers={"Cache-Control": "no-store"})


async def _active_user(request: Request, warden_id: str) -> Any:
    """The active warden user ``warden_id`` names, or None."""
    runtime = _runtime(request)
    if runtime is None:
        return None
    from warden.identity import UserStatus

    try:
        user = await runtime.identity.users.get(uuid.UUID(str(warden_id)))
    except ValueError:
        return None
    return user if user is not None and user.status is UserStatus.ACTIVE else None


# -- Sharing ----------------------------------------------------------------------------------


async def _announce(request: Request, conversation_id: int, event: str, data: dict) -> None:
    from spark.web.live import bus_for

    await bus_for(request.app).publish(conversation_id, event, data)


async def _audit(request: Request, action: str, conversation_id: int, **details: Any) -> None:
    from spark.identity import audit

    await audit.emit_request(
        request,
        action,
        resource_type="conversation",
        resource_id=str(conversation_id),
        details=details,
    )


async def members_view(request: Request, access: ConversationAccess) -> list[dict[str, Any]]:
    """The conversation's people for the page: guid, name, initials, role, colour slot."""
    rows = await conversation_members.members(
        database(request), access.user_guid, int(access.conversation["id"])
    )
    names = await display_names(request, [str(r["user_guid"]) for r in rows])
    return [
        {
            "user_guid": r["user_guid"],
            "role": r["role"],
            "name": names[str(r["user_guid"])]["name"],
            "initials": names[str(r["user_guid"])]["initials"],
            # The owner is colour 1, the others in the order they joined.
            "colour": index % 6 + 1,
            "is_me": r["user_guid"] == access.user_guid,
            "is_new": r["last_seen_at"] is None,
        }
        for index, r in enumerate(rows)
    ]


async def _shareable(request: Request, conversation_id: int) -> ConversationAccess | JSONResponse:
    """The conversation, when sharing applies (server mode, a standard conversation) and the
    signed-in user owns it."""
    if not is_server():
        return not_found()
    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    if (access.conversation.get("conversation_type") or "standard") != "standard":
        return JSONResponse({"error": "Debates and panels cannot be shared"}, status_code=400)
    return access


def _role(value: Any) -> Role | None:
    try:
        role = Role(str(value))
    except ValueError:
        return None
    return role if role in (Role.CONTRIBUTOR, Role.VIEWER) else None


def _link(request: Request, conversation_id: int) -> str:
    runtime = _runtime(request)
    base = str(runtime.config.http.public_base_url).rstrip("/") if runtime is not None else ""
    return f"{base}/chat/{conversation_id}"


async def _notify_by_mail(
    request: Request, user: Any, access: ConversationAccess, kind: str, role: str
) -> None:
    from spark.identity import mail

    sharer = await display_names(request, [access.user_guid])
    await mail.send_share_notice(
        request.app.state.ctx.secrets,
        user.email,
        kind=kind,
        sharer=sharer[access.user_guid]["name"],
        conversation=str(access.conversation.get("name") or "Conversation"),
        role="a contributor" if role == Role.CONTRIBUTOR.value else "read-only",
        link=_link(request, int(access.conversation["id"])),
    )


async def _member_guids(request: Request, access: ConversationAccess) -> set[str]:
    rows = await conversation_members.members(
        database(request), access.user_guid, int(access.conversation["id"])
    )
    return {str(r["user_guid"]) for r in rows}


@router.get("/api/{conversation_id}/members")
async def list_members(request: Request, conversation_id: int) -> JSONResponse:
    """API: the people with access to a conversation the signed-in user may read."""
    if not is_server():
        return not_found()
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    return JSONResponse(
        {"role": access.role.value, "members": await members_view(request, access)},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/{conversation_id}/members")
async def share_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """API: share with a person (``user_id``, a warden user) or change a member's role
    (``user_guid``), as ``role`` contributor or viewer. The owner only."""
    access = await _shareable(request, conversation_id)
    if not isinstance(access, ConversationAccess):
        return access
    data = await request.json()
    role = _role(data.get("role"))
    if role is None:
        return JSONResponse({"error": "role must be contributor or viewer"}, status_code=400)
    db = database(request)
    user = None
    if data.get("user_id"):
        user = await _active_user(request, str(data["user_id"]))
        if user is None:
            return JSONResponse({"error": "No active user found"}, status_code=400)
        target = await user_identities.user_guid_for(db, str(user.id))
    else:
        target = str(data.get("user_guid") or "")
        if target not in await _member_guids(request, access):
            return JSONResponse({"error": "Not a member"}, status_code=400)
    try:
        change = await conversation_members.share(
            db, access.user_guid, conversation_id, target, role
        )
    except NotPermitted:
        return forbidden()
    except NotOwned:
        return not_found()
    except ValueError as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)

    from spark.identity import audit

    if change.outcome == "added":
        await _audit(
            request, audit.CONVERSATION_SHARED, conversation_id, member=target, role=role.value
        )
        if user is not None:
            await _notify_by_mail(request, user, access, "shared", role.value)
    elif change.outcome == "changed":
        await _audit(
            request,
            audit.CONVERSATION_ROLE_CHANGED,
            conversation_id,
            member=target,
            role=role.value,
            previous_role=change.previous_role.value if change.previous_role else None,
        )
    if change.outcome != "unchanged":
        await _announce(request, conversation_id, "members_changed", {})
    return JSONResponse({"status": change.outcome, "members": await members_view(request, access)})


@router.delete("/api/{conversation_id}/members/{member_guid}")
async def remove_member(request: Request, conversation_id: int, member_guid: str) -> JSONResponse:
    """API: the owner removes a person, or a member leaves (their own guid). Takes effect at
    once: their open views close and a turn of theirs is cancelled."""
    if not is_server():
        return not_found()
    me = current_user_guid(request)
    db = database(request)
    role: Role | None
    try:
        if member_guid == me:
            role = await conversation_members.leave(db, me, conversation_id)
        else:
            access = await _shareable(request, conversation_id)
            if not isinstance(access, ConversationAccess):
                return access
            role = await conversation_members.remove_member(db, me, conversation_id, member_guid)
    except NotPermitted:
        return forbidden()
    except NotOwned:
        return not_found()
    except ValueError as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)
    if role is None:
        return not_found()

    from spark.identity import audit

    await _audit(
        request,
        audit.CONVERSATION_MEMBER_REMOVED,
        conversation_id,
        member=member_guid,
        role=role.value,
        left=member_guid == me,
    )
    await _announce(request, conversation_id, "member_removed", {"user_guid": member_guid})
    await _announce(request, conversation_id, "members_changed", {})
    return JSONResponse({"status": "removed"})


@router.post("/api/{conversation_id}/transfer")
async def transfer_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """API: make a member (``user_guid``) or a person added in the transfer (``user_id``)
    the owner. The old owner becomes a contributor; the new owner is told."""
    access = await _shareable(request, conversation_id)
    if not isinstance(access, ConversationAccess):
        return access
    data = await request.json()
    db = database(request)
    if data.get("user_id"):
        user = await _active_user(request, str(data["user_id"]))
        if user is None:
            return JSONResponse({"error": "No active user found"}, status_code=400)
        target = await user_identities.user_guid_for(db, str(user.id))
    else:
        target = str(data.get("user_guid") or "")
        if target not in await _member_guids(request, access):
            return JSONResponse({"error": "Not a member"}, status_code=400)
        member = await _warden_user(request, target)
        user = await _active_user(request, str(member.id)) if member is not None else None
        if user is None:
            return JSONResponse({"error": "No active user found"}, status_code=400)
    try:
        await conversation_members.transfer(db, access.user_guid, conversation_id, target)
    except NotPermitted:
        return forbidden()
    except NotOwned:
        return not_found()
    except ValueError as refused:
        return JSONResponse({"error": str(refused)}, status_code=400)

    from spark.identity import audit

    await _audit(request, audit.CONVERSATION_TRANSFERRED, conversation_id, new_owner=target)
    await _notify_by_mail(request, user, access, "transferred", Role.OWNER.value)
    await _announce(request, conversation_id, "members_changed", {})
    return JSONResponse({"status": "transferred"})
