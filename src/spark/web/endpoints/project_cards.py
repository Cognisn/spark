"""Project cards: the JSON API the chat page acts on them through (PC17, PC18).

A project tool's result carries a card's id (:data:`spark.projects.cards.CARD_MARKER`);
the page reads the card here and the user acts on it here. Routes (under
``/project-cards/api``):

* ``GET /{card_id}``: the card, for anyone who may read its conversation. The turn's
  sender gets what it proposes and whether they can still act (``can_act``); any other
  member sees only its kind and state, because a card can show a project they cannot
  open (its instructions, its members' names). Anyone else: 404.
* ``POST /{card_id}/accept``: Create (a suggestion, with ``edits`` from Edit: ``name``,
  ``description``, ``instructions``), or Confirm (an update or a move). The projects
  service runs for the signed-in user with their rights now; 409 when the card was used,
  turned down or has expired, and 409 with the card when a move's warning changed
  (shown again, nothing moved).
* ``POST /{card_id}/dismiss``: Dismiss a suggestion, Cancel an update or a move.

Only the card's own user, while they may still send in its conversation, acts on it:
anyone else gets 404, as for a card that does not exist. Both state-changing routes are
POST, so the CSRF token and the Origin check apply (:mod:`spark.web.security`). Each
decision is audited in server mode (``spark.project.card_accepted`` or
``card_dismissed``, and ``created_by_tool``, ``updated_by_tool`` or
``conversation_moved`` for what it did).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spark.database.scope import Capability, NotOwned, NotPermitted
from spark.identity import audit
from spark.identity.access import current_user_guid
from spark.projects import cards
from spark.projects.access import ProjectRefused
from spark.projects.cards import CardGone, CardKind, MoveChanged, ProjectCard
from spark.web.db import database
from spark.web.scope import ConversationAccess, conversation_access, not_found

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/project-cards")

_NOT_FOUND = "Card not found"
_NO_STORE = {"Cache-Control": "no-store"}
_EDITABLE = ("name", "description", "instructions")


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


async def _names(request: Request, guids: list[str]) -> dict[str, str]:
    from spark.web.endpoints.conversations import display_names

    found = await display_names(request, guids)
    return {guid: value["name"] for guid, value in found.items()}


async def _view(request: Request, card: ProjectCard, me: str) -> dict[str, Any]:
    state = card.state(cards.now())
    view: dict[str, Any] = {
        "id": card.id,
        "kind": card.kind.value,
        "state": state,
        "conversation_id": card.conversation_id,
        "mine": card.user_guid == me,
        "can_act": card.user_guid == me and state == "pending",
    }
    if card.user_guid != me:
        return view
    view["project_id"] = card.project_id
    view["expires_at"] = card.expires_at
    payload = dict(card.payload)
    if card.kind is CardKind.MOVE:
        people = [*payload.get("widened", {}), *payload.get("narrowed", {})]
        people += list(payload.get("shares_removed", []))
        payload["names"] = await _names(request, people)
    view["payload"] = payload
    return view


async def _card(request: Request, card_id: str, need: Capability) -> ProjectCard | JSONResponse:
    card = await cards.get_card(database(request), card_id)
    if card is None:
        return not_found(_NOT_FOUND)
    access = await conversation_access(request, card.conversation_id, need)
    if not isinstance(access, ConversationAccess):
        return not_found(_NOT_FOUND)
    return card


@router.get("/api/{card_id}")
async def get_card(request: Request, card_id: str) -> JSONResponse:
    """The card, as its sender or another member of its conversation sees it."""
    card = await _card(request, card_id, Capability.READ)
    if not isinstance(card, ProjectCard):
        return card
    view = await _view(request, card, current_user_guid(request))
    return JSONResponse({"card": view}, headers=_NO_STORE)


def _edits(data: Any, card: ProjectCard) -> dict[str, str] | JSONResponse:
    raw = data.get("edits") if isinstance(data, dict) else None
    if raw is None:
        return {}
    if card.kind is not CardKind.SUGGESTION:
        return _error(400, "Only a suggested project can be edited before it is created.")
    if not isinstance(raw, dict) or any(k not in _EDITABLE for k in raw):
        return _error(400, "edits may change only name, description and instructions.")
    return {k: str(v) for k, v in raw.items() if v is not None}


async def _audit(request: Request, action: str, resource: str, rid: Any, **details: Any) -> None:
    await audit.emit_request(
        request, action, resource_type=resource, resource_id=str(rid), details=details
    )


async def _announce(request: Request, conversation_id: int, report: dict[str, Any]) -> None:
    """Tell the conversation's open views who lost sight of it, as a move through the
    projects API does."""
    from spark.web.live import bus_for

    bus = bus_for(request.app)
    for guid, now in dict(report.get("narrowed") or {}).items():
        if now is None:
            await bus.publish(conversation_id, "member_removed", {"user_guid": guid})
    await bus.publish(conversation_id, "members_changed", {})


async def _record(request: Request, done: cards.Accepted) -> None:
    card = done.card
    await _audit(
        request,
        audit.PROJECT_CARD_ACCEPTED,
        "project_card",
        card.id,
        kind=card.kind.value,
        conversation_id=card.conversation_id,
        project_id=done.project_id,
    )
    if card.kind is CardKind.SUGGESTION:
        await _audit(
            request,
            audit.PROJECT_CREATED_BY_TOOL,
            "project",
            done.project_id,
            conversation_id=card.conversation_id,
            card=card.id,
        )
    elif card.kind is CardKind.UPDATE:
        await _audit(
            request,
            audit.PROJECT_UPDATED_BY_TOOL,
            "project",
            done.project_id,
            fields=sorted(set(done.changed)),
            card=card.id,
        )
    elif done.move is not None:
        move = done.move
        await _audit(
            request,
            audit.PROJECT_CONVERSATION_MOVED,
            "project",
            move.get("to_project") or move.get("from_project") or 0,
            conversation_id=card.conversation_id,
            from_project=move.get("from_project"),
            to_project=move.get("to_project"),
            widened=len(move.get("widened") or {}),
            narrowed=len(move.get("narrowed") or {}),
            card=card.id,
        )
        await _announce(request, card.conversation_id, move)


@router.post("/api/{card_id}/accept")
async def accept_card(request: Request, card_id: str) -> JSONResponse:
    """Create (with any edits) or Confirm: the projects service runs now, for the user."""
    card = await _card(request, card_id, Capability.CONTRIBUTE)
    if not isinstance(card, ProjectCard):
        return card
    try:
        data = await request.json()
    except ValueError:
        data = {}
    edits = _edits(data, card)
    if isinstance(edits, JSONResponse):
        return edits
    me = current_user_guid(request)
    try:
        done = await cards.accept(database(request), me, card_id, edits=edits)
    except NotOwned:
        return not_found(_NOT_FOUND)
    except NotPermitted as exc:
        text = str(exc)
        return _error(403, "Only the project's owner can do that." if " may not " in text else text)
    except MoveChanged as changed:
        return _error(409, str(changed), card=await _view(request, changed.card, me))
    except CardGone as gone:
        fresh = await cards.get_card(database(request), card_id)
        view = await _view(request, fresh, me) if fresh is not None else None
        return _error(409, str(gone), card=view)
    except ProjectRefused as exc:
        return _error(400, str(exc))
    await _record(request, done)
    return JSONResponse(
        {
            "card": await _view(request, done.card, me),
            "project_id": done.project_id,
            "changed": done.changed,
            "move": done.move,
        }
    )


@router.post("/api/{card_id}/dismiss")
async def dismiss_card(request: Request, card_id: str) -> JSONResponse:
    """Dismiss a suggestion, or Cancel an update or a move. Nothing changes."""
    card = await _card(request, card_id, Capability.CONTRIBUTE)
    if not isinstance(card, ProjectCard):
        return card
    me = current_user_guid(request)
    try:
        done = await cards.decline(database(request), me, card_id)
    except NotOwned:
        return not_found(_NOT_FOUND)
    except CardGone as gone:
        fresh = await cards.get_card(database(request), card_id)
        view = await _view(request, fresh, me) if fresh is not None else None
        return _error(409, str(gone), card=view)
    await _audit(
        request,
        audit.PROJECT_CARD_DISMISSED,
        "project_card",
        done.id,
        kind=done.kind.value,
        status=done.status.value,
        conversation_id=done.conversation_id,
    )
    return JSONResponse({"card": await _view(request, done, me)})
