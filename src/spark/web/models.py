"""The models a user may choose: those their connections offer (M3 to M9).

A model is a (connection, model id) pair, kept as ``connection/model``
(:mod:`spark.connections.refs`). The models offered are each running connection's
enabled models (:class:`spark.connections.runtime.ConnectionRuntime`); on a server
a user sees only the connections assigned to them or to their roles, and an
administrator also sees connections not yet assigned to anyone. Personal mode's
single user sees every connection.

Every endpoint where a user chooses a model asks :func:`not_offered` first and
answers 409, naming the model, when it is not offered to that user. The rule is
the model list's own (:func:`listed`).

A conversation pinned to a model that has since left the list keeps working;
:func:`notice` gives the message its page shows.

:func:`default_model` is the model a user's choices start from: on a server the
signed-in user's ``default_model.model_id`` preference (else the instance's),
only when offered; in personal mode ``config.yaml``'s, as before.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from starlette.requests import Request

__all__ = [
    "connections",
    "context_window",
    "default_model",
    "default_model_mode",
    "is_offered",
    "listed",
    "not_offered",
    "notice",
    "offered_models",
    "refusal_message",
    "user_scope",
]


def connections(request: Request) -> Any:
    """The app's connection runtime, or None before start-up has made it."""
    return getattr(request.app.state, "connections", None)


def user_scope(request: Request) -> Any:
    """The signed-in user as the assignment check sees them (None in personal mode)."""
    if not _server():
        return None
    from spark.connections.runtime import UserScope
    from spark.identity.access import current_identity, current_user_guid, has_permission
    from spark.identity.permissions import SETTINGS_INSTANCE

    identity = current_identity(request)
    roles = frozenset(identity.roles) if identity is not None else frozenset()
    return UserScope(
        current_user_guid(request),
        roles,
        administrator=has_permission(request, SETTINGS_INSTANCE),
    )


def listed(request: Request) -> list[dict[str, Any]]:
    """The models the signed-in user may choose, grouped by connection."""
    runtime = connections(request)
    if runtime is None:
        return []
    return list(runtime.offered_models(user_scope(request)))


def offered_models(request: Request) -> frozenset[str]:
    """The ids (``connection/model``) of the models the signed-in user may choose."""
    return frozenset(str(m["id"]) for m in listed(request))


def is_offered(request: Request, model_id: str) -> bool:
    """Whether the signed-in user may choose ``model_id`` (every model before start-up)."""
    runtime = connections(request)
    if runtime is None:
        return True
    return bool(runtime.is_offered(model_id, user_scope(request)))


def context_window(request: Request, model_id: str) -> int:
    """The model's context window, from the catalogue."""
    runtime = connections(request)
    if runtime is None or not model_id:
        from spark.connections.runtime import FALLBACK_LIMITS

        return FALLBACK_LIMITS[0]
    return int(runtime.limits(model_id)[0])


def refusal_message(model_id: str) -> str:
    return (
        f"The model '{model_id}' is not offered on this instance. "
        "Choose one of the models listed."
    )


def not_offered(request: Request, *model_ids: object) -> JSONResponse | None:
    """409 for the first chosen model that is not offered; None when every one is.

    Empty or missing choices are left to the endpoint's own validation.
    """
    for model_id in model_ids:
        if isinstance(model_id, str) and model_id and not is_offered(request, model_id):
            return JSONResponse(
                {"error": refusal_message(model_id), "model_id": model_id}, status_code=409
            )
    return None


def notice(request: Request, model_id: str | None) -> str | None:
    """The notice for a conversation pinned to a model no longer offered (None otherwise)."""
    if not model_id or is_offered(request, model_id):
        return None
    return (
        f"This conversation uses {model_id}, which is no longer offered on this instance. "
        "It keeps working here; choose an offered model to change it."
    )


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


def _config(request: Request, key: str, default: Any) -> Any:
    """Personal mode: a value from ``config.yaml`` (konfig), as before."""
    ctx = getattr(request.app.state, "ctx", None)
    return ctx.settings.get(key, default) if ctx is not None else default


async def _instance(request: Request, key: str) -> Any:
    """Server mode: an instance setting (never ``config.yaml``)."""
    import spark.instance_settings as instance_settings
    from spark.web.db import database

    source = await instance_settings.server_settings(
        request.app, database(request), request.app.state.ctx.settings
    )
    return source.get(key)


async def default_model(request: Request, user_guid: str | None) -> str:
    """The model a user's choices start from ("" when there is none).

    A server answers the user's ``default_model.model_id`` preference, else the
    instance's, each only when it is offered. Personal mode answers
    ``config.yaml``'s, as before.
    """
    if not _server():
        return _as_ref(request, str(_config(request, "default_model.model_id", "") or ""))
    from spark.instance_settings import accessor
    from spark.web.db import database

    candidates: list[Any] = []
    if user_guid:
        prefs = await accessor.preferences(database(request), user_guid)
        candidates.append(prefs.get("default_model.model_id"))
    candidates.append(await _instance(request, "default_model.model_id"))
    for model_id in candidates:
        if isinstance(model_id, str) and model_id and is_offered(request, model_id):
            return _as_ref(request, model_id)
    return ""


def _as_ref(request: Request, model_id: str) -> str:
    """``model_id`` as the ``connection/model`` the model list names it by (a bare id
    from before connections included); as it is when no connection offers it."""
    runtime = connections(request)
    ref = (
        runtime.resolve(model_id, user_scope(request)) if runtime is not None and model_id else None
    )
    return str(ref) if ref is not None else model_id


async def default_model_mode(request: Request) -> str:
    """``default`` or ``mandatory``: the instance setting on a server, ``config.yaml``'s otherwise."""
    if _server():
        return str(await _instance(request, "default_model.mode") or "default")
    return str(_config(request, "default_model.mode", "default") or "default")
