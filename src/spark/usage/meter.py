"""Metering: every model call a turn makes is billed to the turn's user (A14).

conduit emits one ``model_call_finished`` event for every model call, with its
four token counts, and SPARK's event sink (:mod:`spark.connections.usage`) turns
each attributed call into one ``usage_tracking`` row: the user, the connection,
the model, the source (the task: chat, compaction, agent, debate, panel or
action), the conversation or action, and the uncached input, output, cache read
and cache write tokens apart.

:func:`meter` attributes a service: it returns the same model's service with its
calls billed to ``user_guid`` under ``source``. A service that cannot be
attributed (anything other than a connection service) is returned unchanged and
its calls are not recorded.
"""

from __future__ import annotations

from typing import Any, overload

from spark.connections.service import ConnectionService
from spark.connections.usage import Attribution
from spark.database import usage

__all__ = ["meter"]


@overload
def meter(
    service: None,
    db: object = None,
    *,
    user_guid: str,
    source: str,
    model_id: str = "",
    conversation_id: int | None = None,
    action_id: int | None = None,
) -> None: ...


@overload
def meter(
    service: object,
    db: object = None,
    *,
    user_guid: str,
    source: str,
    model_id: str = "",
    conversation_id: int | None = None,
    action_id: int | None = None,
) -> Any: ...


def meter(
    service: object | None,
    db: object = None,
    *,
    user_guid: str,
    source: str,
    model_id: str = "",
    conversation_id: int | None = None,
    action_id: int | None = None,
) -> Any:
    """``service`` with its calls billed to ``user_guid`` under ``source`` (None stays None).

    ``db`` and ``model_id`` are accepted for the callers' convenience: the database
    write is the event sink's, and the model is the one each call names.
    """
    if source not in usage.SOURCES:
        raise ValueError(f"unknown usage source {source!r}")
    if service is None:
        return None
    if not isinstance(service, ConnectionService):
        return service
    return service.attributed(
        Attribution(
            user_guid=user_guid,
            source=source,
            conversation_id=conversation_id,
            action_id=action_id,
        )
    )
