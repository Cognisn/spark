"""The models a user may choose: only those offered (``models.offered``, D38).

Every endpoint where a user chooses a model asks :func:`not_offered` first and
answers 409, naming the model, when the instance does not offer it. The rule is
the model list's own (:meth:`spark.llm.manager.LLMManager.is_offered`): a server
limits both to ``models.offered`` when it is set, and personal mode, which has
no such limit, offers every model as before.

A conversation pinned to a model that has since left the list keeps working;
:func:`notice` gives the message its page shows.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from starlette.requests import Request

__all__ = ["is_offered", "not_offered", "notice", "offered_models", "refusal_message"]


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
