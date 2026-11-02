"""Endpoint helpers for user scoping and for routes a server must not serve.

An id that is not the signed-in user's answers 404, exactly as an id that does
not exist: a 403 would tell one user that another user's conversation, action
or debate exists. The repositories scope every read and write by user as well
(:mod:`spark.database.scope`); these checks give the endpoints their 404.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.requests import Request

from spark.database import autonomous_actions, conversations
from spark.db.engine import Row
from spark.identity.access import current_user_guid
from spark.web.db import database

__all__ = [
    "is_server",
    "not_found",
    "owned_action",
    "owned_conversation",
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


def server_refusal() -> JSONResponse | None:
    """404 on a server for a route that acts on the host process; None in personal mode."""
    return not_found() if is_server() else None


async def owned_conversation(request: Request, conversation_id: int) -> Row | None:
    """The signed-in user's conversation ``conversation_id``, or None."""
    return await conversations.get_conversation(
        database(request), conversation_id, current_user_guid(request)
    )


async def owned_action(request: Request, action_id: int) -> Row | None:
    """The signed-in user's action ``action_id``, or None."""
    return await autonomous_actions.get_action(
        database(request), action_id, current_user_guid(request)
    )
