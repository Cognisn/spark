"""Server-Sent Events streaming for real-time chat responses, and live views of a turn.

A turn runs as its sender (CS28) and one at a time per conversation (CS29): the
sender takes the conversation's turn lock (:class:`RunningTurn`, a lease in
``conversation_turns`` refreshed every :data:`HEARTBEAT_SECONDS`), so a second
sender on any replica is refused with 409 and the holder's name. The lock is given
back when the turn ends, however it ends (completed, cancelled or failed), even if
the sender's page has gone.

While the turn runs its reply text and tool activity are published on the app's
live bus (:mod:`spark.web.live`); every member viewing the conversation follows it
through ``GET /stream/conversation`` and reloads the history when the turn ends.
Removing the sender cancels their turn; removing a viewer closes their view.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from spark.core.cancellation import CancellationToken
from spark.database import conversation_members
from spark.database.conversation_members import HEARTBEAT_SECONDS
from spark.database.scope import Capability, conversation_role
from spark.identity.access import current_user_guid
from spark.usage.budgets import BudgetExceeded
from spark.web import interactions
from spark.web.db import database, sync_database
from spark.web.live import DELTA_INTERVAL, TurnHandle, bus_for
from spark.web.scope import ConversationAccess, conversation_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream")


def _with_inspection(payload: dict[str, Any], event_data: dict[str, Any]) -> dict[str, Any]:
    """Carry a tool result's inspection hit (if any) through to the chat sidecar."""
    if event_data.get("inspection"):
        payload["inspection"] = event_data["inspection"]
    return payload


def _sse_event(evt: dict[str, Any]) -> dict[str, Any] | None:
    """Map one event queued by the turn to the SSE event the page receives.

    The polling loop and the drain after the turn both use it, so an event of
    any kind is sent the same way whichever of them finds it. Returns None for
    a kind the page does not take.
    """
    event_type = evt["event"]
    event_data = evt["data"]

    if event_type == "tool_call":
        return {
            "event": "tool_start",
            "data": json.dumps(
                {
                    "tool_use_id": event_data.get("tool_use_id", ""),
                    "tool_name": event_data.get("tool_name", ""),
                    "params": event_data.get("params", {}),
                }
            ),
        }
    elif event_type == "tool_result":
        return {
            "event": "tool_complete",
            "data": json.dumps(
                _with_inspection(
                    {
                        "tool_use_id": event_data.get("tool_use_id", ""),
                        "tool_name": event_data.get("tool_name", ""),
                        "result": event_data.get("result", ""),
                        "status": event_data.get("status", "success"),
                    },
                    event_data,
                )
            ),
        }
    elif event_type == "reasoning":
        # A model's reasoning, shown beside its reply and never as part of it.
        return {
            "event": "reasoning",
            "data": json.dumps({"text": event_data.get("text", "")}),
        }
    elif event_type == "permission_request":
        return {
            "event": "permission_request",
            "data": json.dumps(event_data),
        }
    elif event_type == "compaction_start":
        return {
            "event": "compaction_status",
            "data": json.dumps({"status": "start", **event_data}),
        }
    elif event_type == "compaction_complete":
        return {
            "event": "compaction_status",
            "data": json.dumps({"status": "complete", **event_data}),
        }
    elif event_type == "tool_iteration_complete":
        return {"event": "progress", "data": json.dumps(event_data)}
    elif event_type == "agent_start":
        return {
            "event": "agent_start",
            "data": json.dumps(
                {
                    "agent_id": event_data.get("agent_id", ""),
                    "agent_name": event_data.get("agent_name", ""),
                    "task": event_data.get("task", ""),
                    "model_id": event_data.get("model_id", ""),
                    "mode": event_data.get("mode", ""),
                }
            ),
        }
    elif event_type == "agent_tool_call":
        return {
            "event": "agent_tool_call",
            "data": json.dumps(
                {
                    "agent_id": event_data.get("agent_id", ""),
                    "tool_name": event_data.get("tool_name", ""),
                    "params": event_data.get("params", {}),
                }
            ),
        }
    elif event_type == "agent_tool_result":
        return {
            "event": "agent_tool_result",
            "data": json.dumps(
                _with_inspection(
                    {
                        "agent_id": event_data.get("agent_id", ""),
                        "tool_name": event_data.get("tool_name", ""),
                        "result": event_data.get("result", ""),
                        "status": event_data.get("status", "success"),
                    },
                    event_data,
                )
            ),
        }
    elif event_type == "agent_complete":
        return {
            "event": "agent_complete",
            "data": json.dumps(
                {
                    "agent_id": event_data.get("agent_id", ""),
                    "agent_name": event_data.get("agent_name", ""),
                    "status": event_data.get("status", "completed"),
                    "result": event_data.get("result", ""),
                    "input_tokens": event_data.get("input_tokens", 0),
                    "output_tokens": event_data.get("output_tokens", 0),
                }
            ),
        }
    elif event_type == "agent_model_approval":
        return {
            "event": "agent_model_approval",
            "data": json.dumps(event_data),
        }
    return None


@router.get("/chat")
async def stream_chat(request: Request) -> EventSourceResponse:
    """SSE endpoint for streaming chat responses."""
    message = request.query_params.get("message", "")
    conversation_id = int(request.query_params.get("conversation_id", "0"))

    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:

        async def error_gen() -> AsyncGenerator[dict[str, Any], None]:
            yield {"event": "error", "data": json.dumps({"message": "Not initialised"})}

        return EventSourceResponse(error_gen())

    user_guid = current_user_guid(request)
    from spark.web.endpoints.chat import refuse_send
    from spark.web.endpoints.usage import request_roles as _request_roles

    access = await conversation_access(request, conversation_id, Capability.CONTRIBUTE)
    if not isinstance(access, ConversationAccess):
        if access.status_code == 403:
            await refuse_send(request, conversation_id, "role")
        return access  # type: ignore[return-value]

    # The sender must be able to use the conversation's model (M9, CS28): refused
    # before anything of the turn is stored, and before the turn lock is taken.
    from spark.identity.access import request_permissions as _request_permissions
    from spark.web.scope import is_server

    refusal = (
        await asyncio.to_thread(
            conv_mgr.model_refusal,
            access.conversation.get("model_id") or "",
            user_guid,
            _request_roles(request),
            _request_permissions(request),
        )
        if is_server()
        else None
    )
    if refusal is not None:
        await refuse_send(request, conversation_id, "model")
        return JSONResponse({"error": refusal}, status_code=403)  # type: ignore[return-value]

    # Resolve an explicit /skill-name invocation at the start of the message
    from spark.skills.trigger import resolve_trigger_for_conversation

    message = await resolve_trigger_for_conversation(
        database(request), user_guid, conversation_id, message
    )

    # Files attached in the composer: the user's own only, named in the message by id.
    from spark.web.attachments import attach

    attached = await attach(request, user_guid, request.query_params.get("attachments"), message)
    if isinstance(attached, JSONResponse):
        return attached  # type: ignore[return-value]
    message = attached

    # Allocate a unique stream id and per-turn cancellation token.
    stream_id = uuid.uuid4().hex
    turn_token = CancellationToken()
    agent_tokens_for_stream: dict[str, CancellationToken] = {}

    # One turn at a time in a conversation, on every replica (CS29). Personal mode has
    # one person and no sharing: its turns run as before.
    try:
        turn: RunningTurn | LocalTurn = (
            await RunningTurn.start(request, conversation_id, user_guid, stream_id, turn_token)
            if is_server()
            else LocalTurn(turn_token)
        )
    except TurnRefused as busy:
        await refuse_send(request, conversation_id, "turn")
        return JSONResponse(  # type: ignore[return-value]
            {"error": str(busy), "holder": busy.holder}, status_code=409
        )

    # Initialise cancellation registries on app.state if absent.
    if not hasattr(request.app.state, "turn_cancel_tokens"):
        request.app.state.turn_cancel_tokens = {}
    if not hasattr(request.app.state, "agent_cancel_tokens"):
        request.app.state.agent_cancel_tokens = {}
    if not hasattr(request.app.state, "stream_agent_sets"):
        request.app.state.stream_agent_sets = {}

    request.app.state.turn_cancel_tokens[stream_id] = turn_token
    request.app.state.stream_agent_sets[stream_id] = agent_tokens_for_stream
    # Every pending id of this stream belongs to its user (server mode: 404 for others).
    interactions.bind(request.app, interactions.STREAM, stream_id, user_guid)

    # Shared state for permission requests
    pending_permissions: dict[str, threading.Event] = {}
    permission_responses: dict[str, str] = {}

    # Shared state for agent model approval requests
    agent_model_events: dict[str, threading.Event] = {}
    agent_model_responses: dict[str, str] = {}

    # Store on app state so the /permission/respond endpoint can access them
    if not hasattr(request.app.state, "permission_events"):
        request.app.state.permission_events = {}
    if not hasattr(request.app.state, "permission_responses"):
        request.app.state.permission_responses = {}
    if not hasattr(request.app.state, "agent_model_events"):
        request.app.state.agent_model_events = {}
    if not hasattr(request.app.state, "agent_model_responses"):
        request.app.state.agent_model_responses = {}

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        yield {
            "event": "status",
            "data": json.dumps({"status": "processing", "stream_id": stream_id}),
        }

        tool_events: list[dict[str, Any]] = []
        final_content = ""

        def status_callback(event_type: str, data: dict[str, Any]) -> None:
            """Collect tool events from the conversation manager."""
            tool_events.append({"event": event_type, "data": data})
            turn.observe(event_type, data)

        def stream_callback(text: str) -> None:
            nonlocal final_content
            final_content += text
            turn.push_text(text)

        def permission_callback(tool_name: str, tool_input: dict[str, Any]) -> str:
            """Called from the tool execution thread when permission is needed.

            Emits a permission request event and blocks until the user responds
            via the /permission/respond endpoint.
            """
            request_id = str(uuid.uuid4())[:8]

            # Viewers see that the sender is answering a tool prompt (never its input).
            turn.observe("permission_request", {"tool_name": tool_name})
            # Create an event that the main loop will emit as SSE
            tool_events.append(
                {
                    "event": "permission_request",
                    "data": {
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "params": tool_input,
                    },
                }
            )

            # Create a threading event to wait on
            wait_event = threading.Event()
            interactions.bind(request.app, interactions.PERMISSION, request_id, user_guid)
            request.app.state.permission_events[request_id] = wait_event
            request.app.state.permission_responses[request_id] = None

            logger.info(
                "Permission requested for tool %s (id=%s), waiting for user...",
                tool_name,
                request_id,
            )

            # Block until user responds (timeout after 120s)
            if wait_event.wait(timeout=120):
                decision = request.app.state.permission_responses.get(request_id, "denied")
            else:
                decision = "denied"
                logger.warning("Permission request %s timed out for tool %s", request_id, tool_name)

            # Cleanup
            request.app.state.permission_events.pop(request_id, None)
            request.app.state.permission_responses.pop(request_id, None)
            interactions.release(request.app, interactions.PERMISSION, request_id)

            logger.info("Permission for %s: %s", tool_name, decision)
            return str(decision)

        def agent_model_callback(
            agent_name: str,
            task: str,
            suggested_model: str,
            available_models: list[dict[str, Any]],
            justification: str = "",
        ) -> str:
            """Called from the agent spawn thread when auto_select model approval is needed.

            Emits an approval event and blocks until the user responds via
            the /agent/model-approve endpoint.
            """
            request_id = str(uuid.uuid4())[:8]

            tool_events.append(
                {
                    "event": "agent_model_approval",
                    "data": {
                        "request_id": request_id,
                        "agent_name": agent_name,
                        "task": task,
                        "suggested_model": suggested_model,
                        "available_models": available_models,
                        "justification": justification,
                    },
                }
            )

            wait_event = threading.Event()
            interactions.bind(request.app, interactions.AGENT_MODEL, request_id, user_guid)
            request.app.state.agent_model_events[request_id] = wait_event
            request.app.state.agent_model_responses[request_id] = None

            logger.info(
                "Agent model approval requested for '%s' (model=%s, id=%s), waiting...",
                agent_name,
                suggested_model,
                request_id,
            )

            # Block until user responds (timeout after 120s)
            if wait_event.wait(timeout=120):
                approved = request.app.state.agent_model_responses.get(request_id, suggested_model)
            else:
                approved = suggested_model
                logger.warning(
                    "Agent model approval %s timed out — using suggested model %s",
                    request_id,
                    suggested_model,
                )

            # Cleanup
            request.app.state.agent_model_events.pop(request_id, None)
            request.app.state.agent_model_responses.pop(request_id, None)
            interactions.release(request.app, interactions.AGENT_MODEL, request_id)

            logger.info("Agent model approved: %s", approved)
            return str(approved)

        def register_agent_cancel(agent_id: str) -> CancellationToken:
            """Allocate a cancellation token for a spawned agent.

            Stored in both the app-state registry (for the cancel endpoint to
            address by agent_id) and the per-stream set (for cleanup when the
            stream finishes).
            """
            tok = CancellationToken()
            interactions.bind(request.app, interactions.AGENT, agent_id, user_guid)
            request.app.state.agent_cancel_tokens[agent_id] = tok
            agent_tokens_for_stream[agent_id] = tok
            return tok

        # This turn's permission, model approval and agent cancel callbacks travel
        # with the turn: the shared manager is never changed, so concurrent streams
        # never answer each other's prompts.
        from spark.core.conversation_manager import TurnCallbacks
        from spark.identity.access import request_permissions

        callbacks = TurnCallbacks(
            tool_permission=permission_callback,
            agent_model=agent_model_callback,
            agent_cancel_register=register_agent_cancel,
        )

        # The turn's tools follow the signed-in user's roles (server mode), and so
        # does its token budget.
        from spark.web.endpoints.usage import refused_event, request_roles

        permissions = request_permissions(request)
        roles = request_roles(request)
        result_future: asyncio.Future[Any] | None = None
        try:
            loop = asyncio.get_event_loop()
            result_future = loop.run_in_executor(
                None,
                lambda: conv_mgr.send_message(
                    conversation_id,
                    message,
                    user_guid,
                    status_callback=status_callback,
                    stream_callback=stream_callback,
                    cancel_token=turn_token,
                    permissions=permissions,
                    callbacks=callbacks,
                    roles=roles,
                ),
            )
            # The lock is given back when the turn ends, whether or not this page stays.
            turn.finish_after(result_future)

            # Poll for events while waiting for completion
            while not result_future.done():
                if turn.handle.removed.is_set():
                    # The sender was removed from the conversation: their turn is
                    # cancelled and their view closes at once.
                    yield {"event": "removed", "data": json.dumps({})}
                    return
                while tool_events:
                    mapped = _sse_event(tool_events.pop(0))
                    if mapped is not None:
                        yield mapped

                if final_content:
                    yield {
                        "event": "response",
                        "data": json.dumps({"content": final_content, "final": False}),
                    }
                    final_content = ""

                await asyncio.sleep(0.2)

            # Get the final result
            try:
                result = result_future.result()

                # The turn can queue events after the polling loop's last look (the
                # compactor reports at the end of a turn): forward every one still
                # queued, of every kind, in order, before the final event.
                while tool_events:
                    mapped = _sse_event(tool_events.pop(0))
                    if mapped is not None:
                        yield mapped

                if result.get("status") == "cancelled":
                    yield {
                        "event": "cancelled",
                        "data": json.dumps({"reason": turn_token.reason or "user"}),
                    }
                else:
                    yield {
                        "event": "response",
                        "data": json.dumps(
                            {
                                "content": result.get("content", ""),
                                "final": True,
                                "usage": result.get("usage", {}),
                                "tool_calls": len(result.get("tool_calls", [])),
                                "iterations": result.get("iterations", 1),
                                "budget": result.get("budget"),
                            }
                        ),
                    }
                yield {
                    "event": "complete",
                    "data": json.dumps({"status": result.get("status", "ok")}),
                }

            except BudgetExceeded as refused:
                # Refused at turn start: nothing of the turn was stored or sent.
                yield refused_event(refused)
                yield {"event": "complete", "data": json.dumps({"status": "refused"})}
            except Exception as e:
                logger.error("Streaming error: %s", e)
                yield {"event": "error", "data": json.dumps({"message": str(e)})}

        finally:
            if result_future is None:
                # The turn never started (the page went first): give the lock back.
                await turn.finish("not_started")
            # Drop registries for this stream so cancel endpoints can't address
            # stale tokens after the turn finishes.
            request.app.state.turn_cancel_tokens.pop(stream_id, None)
            request.app.state.stream_agent_sets.pop(stream_id, None)
            interactions.release(request.app, interactions.STREAM, stream_id)
            for aid in list(agent_tokens_for_stream):
                request.app.state.agent_cancel_tokens.pop(aid, None)
                interactions.release(request.app, interactions.AGENT, aid)

    return EventSourceResponse(event_generator())


@router.post("/cancel")
async def cancel_stream(request: Request) -> JSONResponse:
    """API: cancel an active stream's turn and all of its child agents."""
    data = await request.json()
    stream_id = data.get("stream_id", "")
    from spark.web.scope import not_found

    if not interactions.belongs_to_caller(request, interactions.STREAM, stream_id):
        return not_found()
    turn_tokens = getattr(request.app.state, "turn_cancel_tokens", {})
    stream_agents = getattr(request.app.state, "stream_agent_sets", {})

    tok = turn_tokens.get(stream_id)
    if tok is not None:
        tok.cancel("user")
        logger.info("Turn cancel requested for stream %s", stream_id)

    for agent_id, agent_tok in (stream_agents.get(stream_id) or {}).items():
        agent_tok.cancel("user")
        logger.info("Cascade cancel for agent %s in stream %s", agent_id, stream_id)

    return JSONResponse({"status": "ok"})


# -- The turn lock and live publishing ---------------------------------------------------------


class TurnRefused(Exception):
    """Another participant's turn is running in the conversation."""

    def __init__(self, message: str, holder: dict[str, str]) -> None:
        super().__init__(message)
        self.holder = holder


class LocalTurn:
    """A personal-mode turn: no lock and no live publishing (one person, nothing shared)."""

    def __init__(self, token: CancellationToken) -> None:
        self.token = token
        self.handle = TurnHandle(0, "", cancel=token.cancel)

    def push_text(self, text: str) -> None:
        return None

    def observe(self, event_type: str, data: dict[str, Any]) -> None:
        return None

    async def finish(self, status: str) -> None:
        return None

    def finish_after(self, future: asyncio.Future[Any]) -> None:
        return None


class RunningTurn:
    """One participant's turn: it holds the conversation's turn lock while it runs, and
    publishes the turn's events to everyone viewing it.

    Made by :meth:`start` (which takes the lock or raises :class:`TurnRefused`). The
    turn's worker thread reports reply text (:meth:`push_text`) and tool activity
    (:meth:`observe`); a task on the event loop publishes them, coalesced every
    :data:`spark.web.live.DELTA_INTERVAL` seconds, and refreshes the lease every
    :data:`HEARTBEAT_SECONDS`. :meth:`finish` (or :meth:`finish_after` a future)
    publishes the last text, gives the lock back and tells viewers the turn ended.
    """

    def __init__(
        self,
        app: Any,
        database_: Any,
        conversation_id: int,
        user_guid: str,
        stream_id: str,
        token: CancellationToken,
    ) -> None:
        self._app = app
        self._db = database_
        self.conversation_id = conversation_id
        self.user_guid = user_guid
        self.stream_id = stream_id
        self.token = token
        self.bus = bus_for(app)
        self.handle = TurnHandle(conversation_id, user_guid, cancel=token.cancel)
        self._lock = threading.Lock()
        self._text: list[str] = []
        self._events: list[tuple[str, dict[str, Any]]] = []
        self._seq = 0
        self._finished = False
        self._ended = asyncio.Event()
        self._pump: asyncio.Task[None] | None = None
        self._finishing: asyncio.Task[None] | None = None

    @classmethod
    async def start(
        cls,
        request: Request,
        conversation_id: int,
        user_guid: str,
        stream_id: str | None = None,
        token: CancellationToken | None = None,
    ) -> RunningTurn:
        """Take the turn lock for ``user_guid`` and announce the turn; :class:`TurnRefused`
        (naming the holder) while another participant's turn runs."""
        turn = cls(
            request.app,
            database(request),
            conversation_id,
            user_guid,
            stream_id or uuid.uuid4().hex,
            token or CancellationToken(),
        )
        holder = await conversation_members.acquire_turn(
            turn._db, user_guid, conversation_id, turn.stream_id, turn.bus.replica_id
        )
        if holder is not None:
            from spark.web.endpoints.conversations import display_names

            guid = str(holder["user_guid"])
            name = (await display_names(request, [guid]))[guid]["name"]
            raise TurnRefused(
                f"Waiting for {name}'s turn to finish", {"user_guid": guid, "name": name}
            )
        turn.bus.register_turn(turn.handle)
        from spark.web.endpoints.conversations import display_names

        name = (await display_names(request, [user_guid]))[user_guid]["name"]
        await turn.bus.publish(
            conversation_id,
            "turn_started",
            {"user_guid": user_guid, "name": name, "stream_id": turn.stream_id},
        )
        turn._pump = asyncio.get_running_loop().create_task(
            turn._run(), name=f"spark-turn-{turn.stream_id}"
        )
        return turn

    # Called from the turn's worker thread.

    def push_text(self, text: str) -> None:
        with self._lock:
            self._text.append(text)

    def observe(self, event_type: str, data: dict[str, Any]) -> None:
        """Note a turn event viewers may see: a tool starting or ending, or a prompt."""
        phase = {
            "tool_call": "start",
            "tool_result": "end",
            "permission_request": "approval",
        }.get(event_type)
        if phase is None:
            return
        event: dict[str, Any] = {"phase": phase, "tool_name": str(data.get("tool_name", ""))}
        if (
            phase == "end"
            and event["tool_name"] == "show_document"
            and data.get("status") != "error"
        ):
            # A document shown opens in every viewer's preview pane (CS39): its card only.
            from dataclasses import asdict

            from spark.preview.service import parse_card

            card = parse_card(str(data.get("result", "")))
            if card is not None and card.conversation_id == self.conversation_id:
                event["card"] = asdict(card)
        with self._lock:
            self._events.append(("tool", event))

    # On the event loop.

    async def _flush(self) -> None:
        with self._lock:
            text = "".join(self._text)
            events, self._events, self._text = self._events, [], []
        for event, data in events:
            await self.bus.publish(
                self.conversation_id, event, {**data, "stream_id": self.stream_id}
            )
        if text:
            self._seq += 1
            await self.bus.publish(
                self.conversation_id,
                "delta",
                {"seq": self._seq, "text": text, "stream_id": self.stream_id},
            )

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        beat = loop.time()
        while not self._finished:
            await asyncio.sleep(DELTA_INTERVAL)
            try:
                await self._flush()
                if loop.time() - beat >= HEARTBEAT_SECONDS:
                    beat = loop.time()
                    if not await conversation_members.heartbeat_turn(
                        self._db, self.conversation_id, self.stream_id
                    ):
                        logger.warning(
                            "Turn %s lost its lease in conversation %s",
                            self.stream_id,
                            self.conversation_id,
                        )
            except Exception:  # noqa: BLE001 - a live view never breaks a turn
                logger.warning("Live publishing of turn %s failed", self.stream_id)

    async def finish(self, status: str) -> None:
        """End the turn: publish what is left, give the lock back, tell the viewers. Once;
        a second call waits until the first has finished."""
        if self._finished:
            await self._ended.wait()
            return
        self._finished = True
        pump, self._pump = self._pump, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(BaseException):
                await pump
        try:
            await self._flush()
        except Exception:  # noqa: BLE001
            logger.warning("Live publishing of turn %s failed", self.stream_id)
        try:
            await conversation_members.release_turn(self._db, self.conversation_id, self.stream_id)
        finally:
            self.bus.unregister_turn(self.handle)
            try:
                await self.bus.publish(
                    self.conversation_id,
                    "turn_finished",
                    {"status": status, "user_guid": self.user_guid, "stream_id": self.stream_id},
                )
            finally:
                self._ended.set()

    def finish_after(self, future: asyncio.Future[Any]) -> None:
        """Finish the turn when ``future`` (the turn's work) is done, however it ends."""

        def done(fut: asyncio.Future[Any]) -> None:
            if fut.cancelled():
                status = "cancelled"
            elif fut.exception() is not None:
                status = "failed"
            else:
                result = fut.result()
                status = str(result.get("status", "ok")) if isinstance(result, dict) else "ok"
            self._finishing = asyncio.ensure_future(self.finish(status))

        future.add_done_callback(done)


# -- Watching a conversation -------------------------------------------------------------------

# Seconds between checks that a watcher still has access (a removal whose notice was lost).
WATCH_RECHECK = 10.0


@router.get("/conversation")
async def watch_conversation(request: Request) -> EventSourceResponse:
    """SSE: follow a conversation the signed-in user may read, live.

    Sends ``ready`` (with the running turn's holder, if any), then the live events
    of :mod:`spark.web.live` as they happen. Closes with ``removed`` when the user
    loses access, and ``deleted`` when the conversation is deleted.
    """
    try:
        conversation_id = int(request.query_params.get("conversation_id", "0"))
    except ValueError:
        conversation_id = 0
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access  # type: ignore[return-value]
    user_guid = access.user_guid
    db = database(request)
    bus = bus_for(request.app)
    sub = bus.subscribe(conversation_id, user_guid)
    holder = await conversation_members.current_turn(db, user_guid, conversation_id)
    ready: dict[str, Any] = {"turn": None}
    if holder is not None:
        from spark.web.endpoints.conversations import display_names

        guid = str(holder["user_guid"])
        ready["turn"] = {
            "user_guid": guid,
            "name": (await display_names(request, [guid]))[guid]["name"],
            "stream_id": holder["stream_id"],
        }

    async def events() -> AsyncGenerator[dict[str, Any], None]:
        try:
            yield {"event": "ready", "data": json.dumps(ready)}
            while True:
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=WATCH_RECHECK)
                except TimeoutError:
                    if await conversation_role(db, user_guid, conversation_id) is None:
                        yield {"event": "removed", "data": json.dumps({})}
                        return
                    continue
                yield {"event": item["event"], "data": json.dumps(item["data"])}
                if sub.closed.is_set() and item["event"] in ("removed", "deleted", "closed"):
                    return
        finally:
            bus.unsubscribe(sub)

    return EventSourceResponse(events())
