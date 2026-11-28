"""A chat turn's pending interactions, each bound to the user whose turn raised it.

A turn's tool permission prompts, agent model approvals, agent cancel tokens and
the turn itself are addressed by id through app-wide registries
(``/chat/permission/respond``, ``/chat/agent/model-approve``,
``/chat/agent/cancel`` and ``/stream/cancel``). On a server one app serves every
user, so each id is bound to its owner's guid when it is created and released
when it ends, and an id that is not the caller's answers 404, exactly as one
that does not exist (:mod:`spark.web.scope`).

Personal mode has one user: every id is the caller's, as before.

A running turn keeps its interactions in a :class:`TurnRecord` for the life of the
turn, not of the page's stream: a sender who moves to another conversation and comes
back can still answer the turn's prompts and stop it. A held prompt waits
:data:`AWAY_LIMIT` seconds while no page of the sender's has the conversation open,
and :data:`SEEN_LIMIT` seconds from the moment one is; it then ends unanswered (the
caller denies the tool, or takes the suggested agent model).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Final

from starlette.requests import Request

from spark.core.cancellation import CancellationToken
from spark.identity.access import optional_user_guid
from spark.web.scope import is_server

__all__ = [
    "AGENT",
    "AGENT_MODEL",
    "AWAY_LIMIT",
    "PERMISSION",
    "PRESENCE_FRESH",
    "SEEN_LIMIT",
    "STREAM",
    "TurnRecord",
    "bind",
    "belongs_to_caller",
    "record_for_conversation",
    "record_for_stream",
    "release",
]

PERMISSION: Final = "permission"
AGENT_MODEL: Final = "agent_model"
AGENT: Final = "agent"
STREAM: Final = "stream"

# Seconds a held prompt waits once a page of the sender's has the conversation open,
# and while none has (Matthew, 28 November 2026). Read at each wait, so tests may
# shorten them.
SEEN_LIMIT: float = 120.0
AWAY_LIMIT: float = 600.0
# Seconds a page's last sign of presence counts as "open".
PRESENCE_FRESH: float = 25.0
# Seconds between a waiting prompt's looks at its answer, the turn and the clock.
WAIT_STEP: float = 0.25

# The attribute of ``app.state`` holding (kind, id) -> owner guid.
_STATE_KEY: Final = "interaction_owners"
# The attribute of ``app.state`` holding stream id -> TurnRecord.
_RECORDS_KEY: Final = "turn_records"

# Where each prompt kind keeps its waiting events and answers on ``app.state`` (read
# by the answer routes, as before the record).
_PROMPT_STATE: Final = {
    PERMISSION: ("permission_events", "permission_responses"),
    AGENT_MODEL: ("agent_model_events", "agent_model_responses"),
}


def _owners(app: Any) -> dict[tuple[str, str], str]:
    owners: dict[tuple[str, str], str] | None = getattr(app.state, _STATE_KEY, None)
    if owners is None:
        owners = {}
        setattr(app.state, _STATE_KEY, owners)
    return owners


def _state_dict(app: Any, name: str) -> dict[str, Any]:
    found: dict[str, Any] | None = getattr(app.state, name, None)
    if found is None:
        found = {}
        setattr(app.state, name, found)
    return found


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


def record_for_stream(app: Any, stream_id: str) -> TurnRecord | None:
    """The running turn ``stream_id`` on this replica, or None."""
    records: dict[str, TurnRecord] = _state_dict(app, _RECORDS_KEY)
    return records.get(stream_id)


def record_for_conversation(app: Any, conversation_id: int) -> TurnRecord | None:
    """The turn running in ``conversation_id`` on this replica, or None."""
    records: dict[str, TurnRecord] = _state_dict(app, _RECORDS_KEY)
    for record in list(records.values()):
        if record.conversation_id == conversation_id:
            return record
    return None


class TurnRecord:
    """One running turn's interactions, from its start until it ends however it ends.

    :meth:`open` registers the turn (its cancel token, bound to the sender) in the
    app's registries; :meth:`close` releases every id the turn ever bound, ends a
    prompt still waiting and forgets the turn. Prompts (:meth:`ask`) and agents
    (:meth:`register_agent`) are called from the turn's worker thread; answers,
    cancels and presence from the event loop. ``publish(event, data)`` sends an
    event to the sender's own pages only (``prompt`` and ``prompt_closed``); it
    must be safe to call from any thread.
    """

    def __init__(
        self,
        app: Any,
        conversation_id: int,
        user_guid: str,
        stream_id: str,
        token: CancellationToken,
        *,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._app = app
        self.conversation_id = conversation_id
        self.user_guid = user_guid
        self.stream_id = stream_id
        self.token = token
        self.publish = publish
        self.started_at = datetime.now(timezone.utc).isoformat()
        # What the turn is doing now, for a page that attaches (a tool, compaction).
        self.activity: dict[str, Any] = {"kind": "thinking"}
        # The reply text published since the last tool started, for a page that attaches.
        self.text = ""
        self.agents: dict[str, CancellationToken] = {}
        self._agent_names: dict[str, str] = {}
        self._prompts: dict[str, dict[str, Any]] = {}
        self._seen_at: float | None = None
        self._lock = threading.Lock()
        self._closed = False

    # -- Lifetime ---------------------------------------------------------------------------

    def open(self) -> None:
        """Register the turn: its cancel token and agents, bound to the sender."""
        bind(self._app, STREAM, self.stream_id, self.user_guid)
        _state_dict(self._app, "turn_cancel_tokens")[self.stream_id] = self.token
        _state_dict(self._app, "stream_agent_sets")[self.stream_id] = self.agents
        _state_dict(self._app, _RECORDS_KEY)[self.stream_id] = self

    def close(self) -> None:
        """The turn has ended: release everything of it, and end any prompt still waiting."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            prompts = list(self._prompts.values())
            agents = list(self.agents)
        for prompt in prompts:
            events_name, _ = _PROMPT_STATE[str(prompt["kind"])]
            event = _state_dict(self._app, events_name).get(str(prompt["request_id"]))
            if event is not None:
                event.set()
        release(self._app, STREAM, self.stream_id)
        turn_tokens = _state_dict(self._app, "turn_cancel_tokens")
        if turn_tokens.get(self.stream_id) is self.token:
            del turn_tokens[self.stream_id]
        _state_dict(self._app, "stream_agent_sets").pop(self.stream_id, None)
        agent_tokens = _state_dict(self._app, "agent_cancel_tokens")
        for agent_id in agents:
            if agent_tokens.get(agent_id) is self.agents.get(agent_id):
                agent_tokens.pop(agent_id, None)
            release(self._app, AGENT, agent_id)
        records = _state_dict(self._app, _RECORDS_KEY)
        if records.get(self.stream_id) is self:
            del records[self.stream_id]

    @property
    def closed(self) -> bool:
        return self._closed

    # -- Cancelling ---------------------------------------------------------------------------

    def register_agent(self, agent_id: str, agent_name: str = "") -> CancellationToken:
        """A cancel token for a spawned agent, bound to the sender, cancelled with the turn."""
        token = CancellationToken()
        bind(self._app, AGENT, agent_id, self.user_guid)
        _state_dict(self._app, "agent_cancel_tokens")[agent_id] = token
        with self._lock:
            self.agents[agent_id] = token
            self._agent_names[agent_id] = agent_name
        return token

    def name_agent(self, agent_id: str, agent_name: str) -> None:
        """Note a running agent's name (from its start event), for a page that attaches."""
        with self._lock:
            if agent_id in self.agents:
                self._agent_names[agent_id] = agent_name

    def agent_done(self, agent_id: str) -> None:
        """The agent finished: it is no longer listed as running (its token stays bound)."""
        with self._lock:
            self._agent_names.pop(agent_id, None)

    def cancel(self, reason: str = "user") -> None:
        """Stop the turn and every agent of it."""
        self.token.cancel(reason)
        with self._lock:
            agents = list(self.agents.values())
        for token in agents:
            token.cancel(reason)

    def cancel_agent(self, agent_id: str, reason: str = "user") -> bool:
        with self._lock:
            token = self.agents.get(agent_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    # -- Prompts ------------------------------------------------------------------------------

    def present(self) -> None:
        """A page of the sender's has the conversation open now."""
        self._seen_at = time.monotonic()

    def _is_present(self, now: float) -> bool:
        seen = self._seen_at
        return seen is not None and now - seen <= PRESENCE_FRESH

    def ask(self, kind: str, request_id: str, data: dict[str, Any]) -> str | None:
        """Hold a prompt for the sender and wait for the answer (on the turn's thread).

        Returns the answer, or None when the prompt ends unanswered: its time ran out,
        the turn was stopped, or the turn ended.
        """
        events_name, answers_name = _PROMPT_STATE[kind]
        events = _state_dict(self._app, events_name)
        answers = _state_dict(self._app, answers_name)
        event = threading.Event()
        prompt = {"kind": kind, "request_id": request_id, "stream_id": self.stream_id, **data}
        with self._lock:
            if self._closed:
                return None
            self._prompts[request_id] = prompt
        events[request_id] = event
        answers[request_id] = None
        bind(self._app, kind, request_id, self.user_guid)
        self._send("prompt", prompt)
        raised = time.monotonic()
        seen_since: float | None = None
        try:
            while True:
                if event.wait(WAIT_STEP):
                    answer = answers.get(request_id)
                    return None if answer is None else str(answer)
                if self.token.is_cancelled() or self._closed:
                    return None
                now = time.monotonic()
                if seen_since is None and self._is_present(now):
                    seen_since = now
                if seen_since is not None:
                    if now - seen_since >= SEEN_LIMIT:
                        return None
                elif now - raised >= AWAY_LIMIT:
                    return None
        finally:
            events.pop(request_id, None)
            answers.pop(request_id, None)
            release(self._app, kind, request_id)
            with self._lock:
                self._prompts.pop(request_id, None)
            self._send("prompt_closed", {"request_id": request_id, "stream_id": self.stream_id})

    def answer(self, kind: str, request_id: str, value: str) -> bool:
        """Answer a prompt of this turn (the first answer wins). False if it is not one."""
        with self._lock:
            prompt = self._prompts.get(request_id)
        if prompt is None or prompt["kind"] != kind:
            return False
        events_name, answers_name = _PROMPT_STATE[kind]
        event = _state_dict(self._app, events_name).get(request_id)
        if event is None or event.is_set():
            return False
        _state_dict(self._app, answers_name)[request_id] = value
        event.set()
        return True

    def _send(self, event: str, data: dict[str, Any]) -> None:
        if self.publish is not None:
            self.publish(event, dict(data))

    # -- For a page that attaches -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The turn's state for its sender's page: what it is doing, its agents and prompts."""
        with self._lock:
            agents = [
                {"agent_id": agent_id, "agent_name": name}
                for agent_id, name in self._agent_names.items()
            ]
            prompts = [dict(p) for p in self._prompts.values()]
        return {
            "stream_id": self.stream_id,
            "started_at": self.started_at,
            "activity": dict(self.activity),
            "agents": agents,
            "prompts": prompts,
            "text": self.text,
        }
