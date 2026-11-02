"""A chat stream's pending interactions, each bound to the user whose turn raised it.

A stream's tool permission prompts, agent model approvals, agent cancel tokens
and the stream itself are addressed by id through app-wide registries
(``/chat/permission/respond``, ``/chat/agent/model-approve``,
``/chat/agent/cancel`` and ``/stream/cancel``). On a server one app serves every
user, so each id is bound to its owner's guid when it is created and released
when it ends, and an id that is not the caller's answers 404, exactly as one
that does not exist (:mod:`spark.web.scope`).

Personal mode has one user: every id is the caller's, as before.
"""

from __future__ import annotations

from typing import Any, Final

from starlette.requests import Request

from spark.identity.access import optional_user_guid
from spark.web.scope import is_server

__all__ = [
    "AGENT",
    "AGENT_MODEL",
    "PERMISSION",
    "STREAM",
    "bind",
    "belongs_to_caller",
    "release",
]

PERMISSION: Final = "permission"
AGENT_MODEL: Final = "agent_model"
AGENT: Final = "agent"
STREAM: Final = "stream"

# The attribute of ``app.state`` holding (kind, id) -> owner guid.
_STATE_KEY: Final = "interaction_owners"


def _owners(app: Any) -> dict[tuple[str, str], str]:
    owners: dict[tuple[str, str], str] | None = getattr(app.state, _STATE_KEY, None)
    if owners is None:
        owners = {}
        setattr(app.state, _STATE_KEY, owners)
    return owners


def bind(app: Any, kind: str, key: str, user_guid: str) -> None:
    """Record that the pending ``kind`` id ``key`` belongs to ``user_guid``."""
    _owners(app)[(kind, key)] = user_guid


def release(app: Any, kind: str, key: str) -> None:
    """Forget the owner of ``key`` (the interaction has ended)."""
    _owners(app).pop((kind, key), None)


def belongs_to_caller(request: Request, kind: str, key: object) -> bool:
    """Whether the request's user may answer or cancel ``key``.

    A server answers only the signed-in owner of a live id; personal mode
    answers every id, as before.
    """
    if not is_server():
        return True
    guid = optional_user_guid(request)
    owner = _owners(request.app).get((kind, str(key)))
    return guid is not None and owner == guid
