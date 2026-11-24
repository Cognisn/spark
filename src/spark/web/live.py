"""Live updates of a shared conversation for everyone viewing it, across web replicas.

One :class:`LiveBus` per app. A page watching a conversation subscribes
(:meth:`LiveBus.subscribe`) and reads events from its own queue; the replica
running a turn publishes the turn's events (:meth:`LiveBus.publish`).

On a server (PostgreSQL) an event is sent with ``pg_notify`` on :data:`CHANNEL`
and every replica, this one included, receives it on its own dedicated LISTEN
connection and hands it to its local subscribers. The LISTEN connection is
opened when the first page on the replica subscribes and closed when the last
one leaves, reconnecting with back-off meanwhile. In personal mode (SQLite) the
bus delivers in-process only.

Events (``event``, ``data``):

* ``turn_started``: ``user_guid``, ``name``: a participant's turn began;
* ``delta``: ``seq``, ``text``: streamed reply text, coalesced (at most every
  :data:`DELTA_INTERVAL` seconds) and split so a payload stays under
  :data:`MAX_PAYLOAD` bytes;
* ``tool``: ``phase`` (``start``, ``end`` or ``approval``), ``tool_name``;
* ``turn_finished``: ``status``: the page reloads the history, the durable record;
* ``members_changed``, ``member_removed`` (``user_guid``), ``conversation_deleted``.

A notification that is missed (a reconnect) loses at most part of a live stream,
never a message: ``turn_finished`` and a reload bring the history back.

``member_removed`` closes that person's subscriptions to the conversation at once,
on every replica, and cancels their turn if this replica runs it
(:meth:`LiveBus.register_turn`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CHANNEL",
    "DELTA_INTERVAL",
    "MAX_PAYLOAD",
    "LiveBus",
    "Subscription",
    "TurnHandle",
    "bus_for",
    "split_text",
]

logger = logging.getLogger(__name__)

CHANNEL = "spark_conversation"
DELTA_INTERVAL = 0.2
MAX_PAYLOAD = 7000  # bytes; PostgreSQL refuses a NOTIFY payload of 8000 or more
APPLICATION_NAME = "spark-conversation-listener"

Connect = Callable[[str], Awaitable[Any]]


def split_text(text: str, limit: int = MAX_PAYLOAD - 512) -> list[str]:
    """``text`` in pieces of at most ``limit`` UTF-8 bytes, never splitting a character."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > limit and current:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        pieces.append("".join(current))
    return pieces


@dataclass(eq=False)
class Subscription:
    """One page watching one conversation: its events arrive on ``queue``.

    ``closed`` is set when the subscription ends from this side (removal, deletion).
    """

    conversation_id: int
    user_guid: str
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = ""


@dataclass(eq=False)
class TurnHandle:
    """A turn running on this replica, so a removal can cancel it."""

    conversation_id: int
    user_guid: str
    cancel: Callable[[str], None]
    removed: threading.Event = field(default_factory=threading.Event)


async def _default_connect(url: str) -> Any:
    import psycopg

    return await psycopg.AsyncConnection.connect(
        url, autocommit=True, application_name=APPLICATION_NAME
    )


class LiveBus:
    """The app's conversation event bus (see the module docstring)."""

    def __init__(
        self,
        *,
        notify: Callable[[str], Awaitable[None]] | None = None,
        listen_url: str | None = None,
        connect: Connect | None = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        idle_timeout: float = 5.0,
    ) -> None:
        self.replica_id = uuid.uuid4().hex
        self._notify = notify
        self._listen_url = listen_url
        self._connect = connect or _default_connect
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._idle_timeout = idle_timeout
        self._subs: dict[int, set[Subscription]] = {}
        self._turns: dict[int, TurnHandle] = {}
        self._task: asyncio.Task[None] | None = None
        self.listening = asyncio.Event()

    @property
    def distributed(self) -> bool:
        """Whether events travel through PostgreSQL to every replica."""
        return self._notify is not None and bool(self._listen_url)

    # -- Subscribers ----------------------------------------------------------------------

    def subscribe(self, conversation_id: int, user_guid: str) -> Subscription:
        """Watch ``conversation_id`` (the caller has checked the user may read it)."""
        sub = Subscription(conversation_id, user_guid)
        self._subs.setdefault(conversation_id, set()).add(sub)
        self._ensure_listening()
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        found = self._subs.get(sub.conversation_id)
        if found is not None:
            found.discard(sub)
            if not found:
                del self._subs[sub.conversation_id]
        self._maybe_stop()

    def _maybe_stop(self) -> None:
        """Close the LISTEN connection once no page watches and no turn runs here."""
        if not self._subs and not self._turns and self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            self.listening.clear()

    def watchers(self, conversation_id: int) -> int:
        """How many pages on this replica watch ``conversation_id``."""
        return len(self._subs.get(conversation_id, ()))

    # -- Turns on this replica ---------------------------------------------------------------

    def register_turn(self, handle: TurnHandle) -> None:
        """A turn runs here: a removal of its sender, from any replica, cancels it."""
        self._turns[handle.conversation_id] = handle
        self._ensure_listening()

    def unregister_turn(self, handle: TurnHandle) -> None:
        if self._turns.get(handle.conversation_id) is handle:
            del self._turns[handle.conversation_id]
        self._maybe_stop()

    # -- Publishing ----------------------------------------------------------------------------

    async def publish(self, conversation_id: int, event: str, data: dict[str, Any]) -> None:
        """Send one event to every page watching ``conversation_id``, on every replica.

        A delta too large for one notification is split into several. Never raises:
        a lost event costs part of a live view, never a message.
        """
        events = [data]
        if event == "delta" and len(json.dumps(data).encode("utf-8")) > MAX_PAYLOAD:
            events = [{**data, "text": piece} for piece in split_text(str(data.get("text", "")))]
        for item in events:
            envelope = {"c": conversation_id, "e": event, "d": item, "r": self.replica_id}
            try:
                if self.distributed:
                    assert self._notify is not None
                    await self._notify(json.dumps(envelope, separators=(",", ":")))
                else:
                    self.deliver(envelope)
            except Exception:  # noqa: BLE001 - a live view never breaks a turn
                logger.warning("Live update for conversation %s not sent", conversation_id)

    def deliver(self, envelope: dict[str, Any]) -> None:
        """Hand one received event to this replica's subscribers (and act on removals)."""
        try:
            conversation_id = int(envelope["c"])
            event = str(envelope["e"])
            data = dict(envelope.get("d") or {})
        except (KeyError, TypeError, ValueError):
            return
        if event == "member_removed":
            removed = str(data.get("user_guid", ""))
            turn = self._turns.get(conversation_id)
            if turn is not None and turn.user_guid == removed:
                turn.removed.set()
                turn.cancel("removed")
        for sub in list(self._subs.get(conversation_id, ())):
            if event == "member_removed" and sub.user_guid == str(data.get("user_guid", "")):
                self._close(sub, "removed")
                continue
            if event == "conversation_deleted":
                self._close(sub, "deleted")
                continue
            sub.queue.put_nowait({"event": event, "data": data})

    def _close(self, sub: Subscription, reason: str) -> None:
        sub.reason = reason
        sub.queue.put_nowait({"event": reason, "data": {}})
        sub.closed.set()

    # -- The LISTEN connection -----------------------------------------------------------------

    def _ensure_listening(self) -> None:
        if not self.distributed or self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run(), name="spark-conversation-listener")

    async def _run(self) -> None:
        delay = self._reconnect_delay
        while True:
            try:
                conn = await self._connect(str(self._listen_url))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep trying while anyone watches
                logger.warning("Conversation listener cannot connect (retrying): %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                continue
            try:
                await conn.execute(f"LISTEN {CHANNEL}")
                self.listening.set()
                delay = self._reconnect_delay
                while True:
                    async for notify in conn.notifies(timeout=self._idle_timeout):
                        with contextlib.suppress(ValueError):
                            self.deliver(json.loads(str(notify.payload)))
                    await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                logger.warning("Conversation listener lost its connection: %s", exc)
            finally:
                self.listening.clear()
                with contextlib.suppress(Exception):
                    await asyncio.shield(conn.close())
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    async def close(self) -> None:
        """Stop listening (tests and shutdown); subscribers are closed."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for subs in list(self._subs.values()):
            for sub in list(subs):
                self._close(sub, "closed")
        self._subs.clear()


def bus_for(app: Any) -> LiveBus:
    """The app's bus, made on first use: distributed on a PostgreSQL server, else local."""
    bus = getattr(app.state, "live_bus", None)
    if isinstance(bus, LiveBus):
        return bus
    from spark.mode import Dialect

    database = getattr(app.state, "database", None)
    settings = getattr(app.state, "database_settings", None)
    notify: Callable[[str], Awaitable[None]] | None = None
    listen_url: str | None = None
    if (
        database is not None
        and settings is not None
        and settings.dialect is Dialect.POSTGRESQL
        and settings.url
    ):
        listen_url = str(settings.url)

        async def send(payload: str) -> None:
            await database.execute("SELECT pg_notify(?, ?)", (CHANNEL, payload))

        notify = send
    bus = LiveBus(notify=notify, listen_url=listen_url)
    app.state.live_bus = bus
    return bus
