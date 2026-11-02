"""The models a user may choose: only those offered (``models.offered``, D38).

Every endpoint where a user chooses a model asks :func:`not_offered` first and
answers 409, naming the model, when the instance does not offer it. The rule is
the model list's own (:meth:`spark.llm.manager.LLMManager.is_offered`): a server
limits both to ``models.offered`` when it is set, and personal mode, which has
no such limit, offers every model as before.

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
    "default_model",
    "default_model_mode",
    "is_offered",
    "not_offered",
    "notice",
    "offered_models",
    "refusal_message",
]


def _manager(request: Request) -> Any:
    return getattr(request.app.state, "llm_manager", None)


def offered_models(request: Request) -> frozenset[str]:
    """The model ids offered; empty when every model is (or no manager is running)."""
    manager = _manager(request)
    return frozenset(manager.offered_models()) if manager is not None else frozenset()


def is_offered(request: Request, model_id: str) -> bool:
    """Whether a user of this app may choose ``model_id``."""
    offered = offered_models(request)
    return not offered or model_id in offered


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
        return str(_config(request, "default_model.model_id", "") or "")
    from spark.instance_settings import accessor
    from spark.web.db import database

    candidates: list[Any] = []
    if user_guid:
        prefs = await accessor.preferences(database(request), user_guid)
        candidates.append(prefs.get("default_model.model_id"))
    candidates.append(await _instance(request, "default_model.model_id"))
    for model_id in candidates:
        if isinstance(model_id, str) and model_id and is_offered(request, model_id):
            return model_id
    return ""


async def default_model_mode(request: Request) -> str:
    """``default`` or ``mandatory``: the instance setting on a server, ``config.yaml``'s otherwise."""
    if _server():
        return str(await _instance(request, "default_model.mode") or "default")
    return str(_config(request, "default_model.mode", "default") or "default")
