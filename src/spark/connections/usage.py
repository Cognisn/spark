"""SPARK's conduit event sink: every model call becomes a usage record (A14, M6).

conduit's provider registry emits exactly one ``model_call_finished`` event per
model call, however the call ends, with its final usage: uncached input, output,
cache read and cache write tokens, apart. SPARK attributes a call before making
it: :meth:`UsageSink.attribute` registers who the call is billed to (the user),
what it is for (the task: chat, compaction, agent, debate, panel or action) and
the conversation or action, under a fresh correlation id that the request
carries. When the event arrives the sink records one usage row with the user,
the connection (the provider's configured name), the model, the task and the
four counts.

A call nobody attributed (a knowledge graph build, which A14 does not meter) is
not recorded. The sink runs on conduit's loop, so it never calls the sync facade
and never blocks that loop: the database write runs on a worker thread through
the database bridge, and finishes before the call's stream does, so the turn that
made the call sees its usage. A failure to record is logged and never breaks the
call it describes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from conduit import Event, ModelCallFinishedEvent

from spark.database import usage as usage_repo

__all__ = ["Attribution", "UsageRecord", "UsageSink", "database_recorder"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who a model call is billed to and what it is for."""

    user_guid: str
    source: str
    conversation_id: int | None = None
    action_id: int | None = None

    def __post_init__(self) -> None:
        if self.source not in usage_repo.SOURCES:
            raise ValueError(f"unknown usage source {self.source!r}")
        if not self.user_guid:
            raise ValueError("a model call is billed to a user")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One finished model call, as SPARK records it."""

    attribution: Attribution
    connection_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


Recorder = Callable[[UsageRecord], Awaitable[None]]


class _Bridge(Protocol):
    """The part of :class:`spark.db.bridge.SyncDatabase` the recorder uses."""

    def call(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any: ...


def database_recorder(db: _Bridge) -> Recorder:
    """A recorder writing through the database bridge, on a worker thread."""

    async def record(rec: UsageRecord) -> None:
        a = rec.attribution
        await asyncio.to_thread(
            db.call,
            usage_repo.record_usage,
            a.conversation_id,
            rec.model_id,
            rec.input_tokens,
            rec.output_tokens,
            a.user_guid,
            source=a.source,
            action_id=a.action_id,
            connection_id=rec.connection_id,
            cache_read_tokens=rec.cache_read_tokens,
            cache_write_tokens=rec.cache_write_tokens,
        )

    return record


class UsageSink:
    """A conduit ``EventSink`` turning attributed ``model_call_finished`` events into usage."""

    def __init__(self, record: Recorder) -> None:
        self._record = record
        self._lock = threading.Lock()
        self._attributed: dict[str, Attribution] = {}

    def attribute(self, attribution: Attribution) -> str:
        """Register a call about to be made; the correlation id its request carries."""
        correlation = f"spark-{uuid.uuid4().hex}"
        with self._lock:
            self._attributed[correlation] = attribution
        return correlation

    def release(self, correlation: str) -> None:
        """Forget a correlation id once its call has finished."""
        with self._lock:
            self._attributed.pop(correlation, None)

    def pending(self) -> int:
        """How many calls are attributed and not yet released (for tests)."""
        with self._lock:
            return len(self._attributed)

    async def emit(self, event: Event) -> None:
        if not isinstance(event, ModelCallFinishedEvent) or event.usage is None:
            return
        correlation = event.correlation_id
        with self._lock:
            attribution = self._attributed.get(correlation) if correlation else None
        if attribution is None:
            return
        usage = event.usage
        record = UsageRecord(
            attribution=attribution,
            connection_id=event.provider,
            model_id=event.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        try:
            await self._record(record)
        except Exception:  # noqa: BLE001 - metering must never break a model call
            logger.error(
                "Could not record usage of %s on %s for a %s call",
                record.model_id,
                record.connection_id,
                attribution.source,
                exc_info=True,
            )
