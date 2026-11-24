"""Endpoint helpers for user scoping and for routes a server must not serve.

An id the signed-in user may not reach answers 404, exactly as an id that does
not exist: a 403 would tell one user that another user's conversation, action
or debate exists. The repositories scope every read and write as well
(:mod:`spark.database.scope`); these checks give the endpoints their answers.

**Conversations** are shared (server mode), so every route that takes a
conversation id asks :func:`conversation_access` for the capability it needs
(read, contribute or manage). A person who is not a member gets 404; a member
whose role does not allow it gets 403, since they already know the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import JSONResponse
from starlette.requests import Request

from spark.database import autonomous_actions, conversations
from spark.database.scope import Capability, Role, allows, conversation_role
from spark.db.engine import Row
from spark.identity.access import current_user_guid
from spark.web.db import database

__all__ = [
    "ConversationAccess",
    "conversation_access",
    "forbidden",
    "is_server",
    "not_found",
    "owned_action",
    "server_refusal",
]


def is_server() -> bool:
    """Whether this process serves many users (server mode)."""
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        return False


def not_found(what: str = "Not found") -> JSONResponse:
    """The answer for an id that does not exist or is another user's."""
    return JSONResponse({"error": what}, status_code=404)


def forbidden(what: str = "Your role in this conversation does not allow this") -> JSONResponse:
    """The answer for a member whose role does not allow what they asked."""
    return JSONResponse({"error": what}, status_code=403)


def server_refusal() -> JSONResponse | None:
    """404 on a server for a route that acts on the host process; None in personal mode."""
    return not_found() if is_server() else None


@dataclass(frozen=True, slots=True)
class ConversationAccess:
    """A conversation the signed-in user may use, with their role in it."""

    conversation: Row
    role: Role
    user_guid: str


async def conversation_access(
    request: Request, conversation_id: int, need: Capability
) -> ConversationAccess | JSONResponse:
    """The conversation and the signed-in user's role, when the role allows ``need``.

    Otherwise the response to return: 404 when they are not a member (or there is no
    such conversation), 403 when they are a member without the capability.
    """
    user_guid = current_user_guid(request)
    db = database(request)
    role = await conversation_role(db, user_guid, conversation_id)
    if role is None:
        return not_found()
    if not allows(role, need):
        return forbidden()
    row = await conversations.get_conversation(db, conversation_id, user_guid)
    if row is None:
        return not_found()
    return ConversationAccess(row, role, user_guid)


async def owned_action(request: Request, action_id: int) -> Row | None:
    """The signed-in user's action ``action_id``, or None."""
    return await autonomous_actions.get_action(
        database(request), action_id, current_user_guid(request)
    )
