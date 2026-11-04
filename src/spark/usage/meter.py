"""Metering: every model call a turn makes is recorded against the turn's user (A14).

:func:`meter` wraps an LLM service in a :class:`MeteredService` that behaves
exactly like it, except that each ``invoke_model`` that returns usage is also
recorded as one ``usage_tracking`` row: the user, the model the service was
last set to, the source (chat, compaction, agent, debate, panel or action) and
the conversation or action. Input tokens include prompt-cache reads and writes,
since the model processed them.

Recording goes through the database bridge, so it works from any worker
thread, and is an insert, so parallel calls for one user never lose tokens. A
failure to record is logged and never breaks the call it describes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, overload

from spark.database import usage
from spark.db.bridge import SyncDatabase

__all__ = ["MeteredService", "meter"]

logger = logging.getLogger(__name__)

_INPUT_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _count(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _tokens(response: Any) -> tuple[int, int] | None:
    """(input, output) tokens a response reports, or None when it reports no usage."""
    if not isinstance(response, Mapping):
        return None
    reported = response.get("usage")
    if not isinstance(reported, Mapping) or not reported:
        return None
    return sum(_count(reported.get(key)) for key in _INPUT_KEYS), _count(
        reported.get("output_tokens")
    )


class MeteredService:
    """An LLM service whose model calls are recorded against one user."""

    def __init__(
        self,
        service: Any,
        db: SyncDatabase,
        *,
        user_guid: str,
        source: str,
        model_id: str = "",
        conversation_id: int | None = None,
        action_id: int | None = None,
    ) -> None:
        if source not in usage.SOURCES:
            raise ValueError(f"unknown usage source {source!r}")
        self._service = service
        self._db = db
        self._user_guid = user_guid
        self._source = source
        self._model_id = model_id
        self._conversation_id = conversation_id
        self._action_id = action_id

    @property
    def wrapped(self) -> Any:
        """The service being metered."""
        return self._service

    def with_source(self, source: str) -> MeteredService:
        """The same service and user, recorded under another source (for example compaction)."""
        return MeteredService(
            self._service,
            self._db,
            user_guid=self._user_guid,
            source=source,
            model_id=self._model_id,
            conversation_id=self._conversation_id,
            action_id=self._action_id,
        )

    def set_model(self, model_id: str) -> Any:
        self._model_id = model_id
        return self._service.set_model(model_id)

    def invoke_model(self, *args: Any, **kwargs: Any) -> Any:
        response = self._service.invoke_model(*args, **kwargs)
        tokens = _tokens(response)
        if tokens is not None:
            self._record(*tokens)
        return response

    def _record(self, input_tokens: int, output_tokens: int) -> None:
        try:
            self._db.call(
                usage.record_usage,
                self._conversation_id,
                self._model_id,
                input_tokens,
                output_tokens,
                self._user_guid,
                source=self._source,
                action_id=self._action_id,
            )
        except Exception:  # noqa: BLE001 - metering must never break a model call
            logger.error(
                "Could not record usage of %s for a %s call",
                self._model_id,
                self._source,
                exc_info=True,
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


@overload
def meter(
    service: None,
    db: SyncDatabase,
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
    db: SyncDatabase,
    *,
    user_guid: str,
    source: str,
    model_id: str = "",
    conversation_id: int | None = None,
    action_id: int | None = None,
) -> MeteredService: ...


def meter(
    service: object | None,
    db: SyncDatabase,
    *,
    user_guid: str,
    source: str,
    model_id: str = "",
    conversation_id: int | None = None,
    action_id: int | None = None,
) -> MeteredService | None:
    """``service`` metered for ``user_guid`` (None stays None; a metered one is re-labelled)."""
    if service is None:
        if source not in usage.SOURCES:
            raise ValueError(f"unknown usage source {source!r}")
        return None
    if isinstance(service, MeteredService):
        service = service.wrapped
    return MeteredService(
        service,
        db,
        user_guid=user_guid,
        source=source,
        model_id=model_id,
        conversation_id=conversation_id,
        action_id=action_id,
    )
