"""Server-Sent Events streaming for real-time chat responses, and live views of a turn.

A turn runs as its sender (CS28) and one at a time per conversation (CS29): the
sender takes the conversation's turn lock (:class:`RunningTurn`; on a server a lease
in ``conversation_turns`` refreshed every :data:`HEARTBEAT_SECONDS`, in personal mode
the turn registered on the app's bus), so a second send is refused with 409 and the
holder's name. The lock is given back when the turn ends, however it ends
(completed, cancelled or failed), even if the sender's page has gone.

A turn outlives its page. Its prompts, its agents and its cancel token are kept in a
:class:`spark.web.interactions.TurnRecord` until it ends, so a sender who moves to
another conversation and comes back finds it still running: the returning page
watches the conversation (``GET /stream/conversation``), is told the turn is its
own and given its state, answers its held prompts and can stop it.

While the turn runs its reply text and tool activity are published on the app's
live bus (:mod:`spark.web.live`); every member viewing the conversation follows it
and reloads the history when the turn ends. The sender's prompts and the turn's
state go to the sender's own pages only. On a server with several replicas an
answer or a stop sent to a replica that does not run the turn travels to the one
that does as a control message on the bus. Removing the sender cancels their turn;
removing a viewer closes their view.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import uuid
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from spark.core.cancellation import CancellationToken
from spark.database import conversation_members
from spark.database.conversation_members import HEARTBEAT_SECONDS
from spark.database.scope import Capability, conversation_role
from spark.identity.access import current_user_guid, optional_user_guid
from spark.usage.budgets import BudgetExceeded
from spark.web import interactions
from spark.web.db import database
from spark.web.live import DELTA_INTERVAL, MAX_PAYLOAD, TurnHandle, bus_for
from spark.web.scope import ConversationAccess, conversation_access, is_server

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


def status_of(work: asyncio.Future[Any]) -> str:
    """How a turn's work ended: its result's status, ``cancelled`` or ``failed``."""
    if work.cancelled():
        return "cancelled"
    if work.exception() is not None:
        return "failed"
    result = work.result()
    return str(result.get("status", "ok")) if isinstance(result, dict) else "ok"


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

    # One turn at a time in a conversation (CS29), on every replica. The turn's record
    # (its prompts, agents and cancel token) lives until the turn ends, not the stream.
    try:
        turn = await RunningTurn.start(request, conversation_id, user_guid, stream_id, turn_token)
    except TurnRefused as busy:
        await refuse_send(request, conversation_id, "turn")
        return JSONResponse(  # type: ignore[return-value]
            {"error": str(busy), "holder": busy.holder}, status_code=409
        )
    record = turn.record

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

            The prompt is held with the turn, offered to every page of the sender's
            that has the conversation open, and answered through /permission/respond.
            """
            request_id = str(uuid.uuid4())[:8]

            # Viewers see that the sender is answering a tool prompt (never its input).
            turn.observe("permission_request", {"tool_name": tool_name})
            # This page's own stream shows the dialog at once.
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
            logger.info(
                "Permission requested for tool %s (id=%s), waiting for user...",
                tool_name,
                request_id,
            )
            answer = record.ask(
                interactions.PERMISSION,
                request_id,
                {"tool_name": tool_name, "params": _fitting(tool_input)},
            )
            if answer is None:
                logger.warning(
                    "Permission request %s for tool %s ended unanswered", request_id, tool_name
                )
            decision = answer or "denied"
            logger.info("Permission for %s: %s", tool_name, decision)
            return decision

        def agent_model_callback(
            agent_name: str,
            task: str,
            suggested_model: str,
            available_models: list[dict[str, Any]],
            justification: str = "",
        ) -> str:
            """Called from the agent spawn thread when auto_select model approval is needed.

            Held with the turn as a tool prompt is, and answered through
            /agent/model-approve.
            """
            request_id = str(uuid.uuid4())[:8]
            data = {
                "request_id": request_id,
                "agent_name": agent_name,
                "task": task,
                "suggested_model": suggested_model,
                "available_models": available_models,
                "justification": justification,
            }
            tool_events.append({"event": "agent_model_approval", "data": data})
            logger.info(
                "Agent model approval requested for '%s' (model=%s, id=%s), waiting...",
                agent_name,
                suggested_model,
                request_id,
            )
            held = {k: v for k, v in data.items() if k != "request_id"}
            answer = record.ask(interactions.AGENT_MODEL, request_id, _fitting_approval(held))
            if answer is None:
                logger.warning(
                    "Agent model approval %s ended unanswered: using suggested model %s",
                    request_id,
                    suggested_model,
                )
            approved = answer or suggested_model
            logger.info("Agent model approved: %s", approved)
            return approved

        def register_agent_cancel(agent_id: str) -> CancellationToken:
            """A cancellation token for a spawned agent, kept with the turn (the cancel
            endpoints address it by agent id until the turn ends)."""
            return record.register_agent(agent_id)

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
                # This page has the conversation open: a held prompt counts from now.
                record.present()
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

                # The turn is over: its lock and registries are given back before the
                # page is told so.
                await turn.finish(status_of(result_future))
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
                await turn.finish("refused")
                yield refused_event(refused)
                yield {"event": "complete", "data": json.dumps({"status": "refused"})}
            except Exception as e:
                logger.error("Streaming error: %s", e)
                await turn.finish("failed")
                yield {"event": "error", "data": json.dumps({"message": str(e)})}

        finally:
            if result_future is None:
                # The turn never started (the page went first): give everything back.
                await turn.finish("not_started")
            # Otherwise the turn's record stays until the turn ends (finish_after), so
            # a page that comes back can still answer its prompts and stop it.

    return EventSourceResponse(event_generator())


def _fitting(params: dict[str, Any]) -> Any:
    """A tool's input as a held prompt carries it: whole, or as a shortened text when it
    would not fit in one live notification."""
    text = json.dumps(params, default=str)
    if len(text.encode("utf-8")) <= _PROMPT_ROOM:
        return params
    return text[: _PROMPT_ROOM // 4] + " ..."


def _fitting_approval(data: dict[str, Any]) -> dict[str, Any]:
    """An agent model approval as a held prompt carries it: the offered models are cut
    (the suggested one kept first) until it fits in one live notification."""
    models = list(data.get("available_models") or [])
    suggested = str(data.get("suggested_model", ""))
    models.sort(key=lambda m: 0 if str(m.get("id", "")) == suggested else 1)
    out = {**data, "task": str(data.get("task", ""))[:1500]}
    out["justification"] = str(data.get("justification", ""))[:1500]
    while True:
        out["available_models"] = models
        if len(json.dumps(out, default=str).encode("utf-8")) <= _PROMPT_ROOM or len(models) <= 1:
            return out
        models = models[:-1]


# Bytes a held prompt may take, leaving room for the envelope within MAX_PAYLOAD.
_PROMPT_ROOM = MAX_PAYLOAD - 1000


async def turn_is_callers(request: Request, conversation_id: int, stream_id: str) -> bool:
    """Whether the request's user holds the running turn ``stream_id`` of
    ``conversation_id``, wherever it runs (the turn lease, server mode)."""
    guid = optional_user_guid(request)
    if guid is None or not stream_id or not conversation_id:
        return False
    holder = await conversation_members.current_turn(database(request), guid, conversation_id)
    return (
        holder is not None
        and str(holder["user_guid"]) == guid
        and str(holder["stream_id"]) == stream_id
    )


def _conversation_id(data: dict[str, Any]) -> int:
    try:
        return int(data.get("conversation_id") or 0)
    except (TypeError, ValueError):
        return 0


@router.post("/cancel")
async def cancel_stream(request: Request) -> JSONResponse:
    """API: cancel a running turn and all of its child agents, from any of the sender's
    pages and on any replica (the replica running the turn acts on it)."""
    data = await request.json()
    stream_id = str(data.get("stream_id", "") or "")
    from spark.web.scope import not_found

    if interactions.belongs_to_caller(request, interactions.STREAM, stream_id):
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

    conversation_id = _conversation_id(data)
    if is_server() and await turn_is_callers(request, conversation_id, stream_id):
        await bus_for(request.app).publish_control(
            conversation_id,
            "cancel",
            {"user_guid": current_user_guid(request), "stream_id": stream_id},
        )
        logger.info("Turn cancel for stream %s sent to the replica running it", stream_id)
        return JSONResponse({"status": "ok"})
    return not_found()


@router.get("/running")
async def running_turns(request: Request) -> JSONResponse:
    """API: the conversations where a turn of the signed-in user is running (the
    "working" marker of the Conversations page and the projects tree)."""
    guid = current_user_guid(request)
    if is_server():
        ids = await conversation_members.running_turns(database(request), guid)
    else:
        ids = bus_for(request.app).turns_of(guid)
    return JSONResponse({"conversations": sorted(int(i) for i in ids)})


# -- The turn lock and live publishing ---------------------------------------------------------


class TurnRefused(Exception):
    """Another turn is running in the conversation."""

    def __init__(self, message: str, holder: dict[str, str]) -> None:
        super().__init__(message)
        self.holder = holder


class RunningTurn:
    """One participant's turn: it holds the conversation's turn lock while it runs, keeps
    its interactions (:attr:`record`) and publishes its events to everyone viewing it.

    Made by :meth:`start` (which takes the lock or raises :class:`TurnRefused`). The
    turn's worker thread reports reply text (:meth:`push_text`) and tool activity
    (:meth:`observe`); a task on the event loop publishes them, coalesced every
    :data:`spark.web.live.DELTA_INTERVAL` seconds, and on a server refreshes the lease
    every :data:`HEARTBEAT_SECONDS`. :meth:`finish` (or :meth:`finish_after` a future)
    publishes the last text, gives the lock and the record back and tells viewers the
    turn ended. Personal mode has no lease: the turn registered on the app's bus is its
    lock.
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
        self._server = is_server()
        self.conversation_id = conversation_id
        self.user_guid = user_guid
        self.stream_id = stream_id
        self.token = token
        self.bus = bus_for(app)
        self.record = interactions.TurnRecord(
            app, conversation_id, user_guid, stream_id, token, publish=self._private
        )
        self.handle = TurnHandle(
            conversation_id,
            user_guid,
            cancel=self.record.cancel,
            stream_id=stream_id,
            control=self._control,
        )
        self._lock = threading.Lock()
        self._text: list[str] = []
        self._events: list[tuple[str, dict[str, Any], str | None]] = []
        self._seq = 0
        self._finished = False
        self._ended = asyncio.Event()
        self._pump: asyncio.Task[None] | None = None
        self._finishing: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

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
        (naming the holder) while another turn runs."""
        turn = cls(
            request.app,
            database(request),
            conversation_id,
            user_guid,
            stream_id or uuid.uuid4().hex,
            token or CancellationToken(),
        )
        from spark.web.endpoints.conversations import display_names

        if turn._server:
            holder = await conversation_members.acquire_turn(
                turn._db, user_guid, conversation_id, turn.stream_id, turn.bus.replica_id
            )
            if holder is not None:
                guid = str(holder["user_guid"])
                name = (await display_names(request, [guid]))[guid]["name"]
                raise TurnRefused(
                    f"Waiting for {name}'s turn to finish", {"user_guid": guid, "name": name}
                )
        elif turn.bus.turn(conversation_id) is not None:
            # Personal mode: one turn at a time per conversation, on this process.
            running = turn.bus.turn(conversation_id)
            assert running is not None
            raise TurnRefused(
                "Your earlier message in this conversation is still being answered",
                {"user_guid": running.user_guid, "name": "You"},
            )
        turn.record.open()
        turn.bus.register_turn(turn.handle)
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
        """Note a turn event: a tool starting or ending, or a prompt, for every viewer;
        an agent or compaction for the sender's own pages; and what the turn is doing,
        for a page of the sender's that attaches."""
        record = self.record
        if event_type == "agent_start":
            agent_id = str(data.get("agent_id", ""))
            name = str(data.get("agent_name", ""))
            record.name_agent(agent_id, name)
            self._private("agent", {"phase": "start", "agent_id": agent_id, "agent_name": name})
            return
        if event_type == "agent_complete":
            agent_id = str(data.get("agent_id", ""))
            record.agent_done(agent_id)
            self._private(
                "agent",
                {
                    "phase": "end",
                    "agent_id": agent_id,
                    "agent_name": str(data.get("agent_name", "")),
                },
            )
            return
        if event_type in ("compaction_start", "compaction_complete"):
            starting = event_type == "compaction_start"
            record.activity = {"kind": "compaction"} if starting else {"kind": "thinking"}
            self._private("compaction", {"phase": "start" if starting else "end"})
            return
        phase = {
            "tool_call": "start",
            "tool_result": "end",
            "permission_request": "approval",
        }.get(event_type)
        if phase is None:
            return
        event: dict[str, Any] = {"phase": phase, "tool_name": str(data.get("tool_name", ""))}
        if data.get("tool_use_id"):
            event["tool_use_id"] = str(data.get("tool_use_id"))
        if phase == "start":
            record.activity = {"kind": "tool", "tool_name": event["tool_name"]}
        elif phase == "end":
            record.activity = {"kind": "thinking"}
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
            if phase == "start" and self._text:
                # The text before the tool reaches the viewers before the tool does.
                self._events.append(("delta", {"text": "".join(self._text)}, None))
                self._text = []
            self._events.append(("tool", event, None))

    def _private(self, event: str, data: dict[str, Any]) -> None:
        """Queue an event for the sender's own pages only (any thread)."""
        with self._lock:
            self._events.append((event, {**data, "stream_id": self.stream_id}, self.user_guid))

    # On the event loop.

    def _control(self, event: str, data: dict[str, Any]) -> None:
        """A control message for this turn, from any replica (the bus has checked it
        names this turn's sender and stream)."""
        record = self.record
        if event == "answer":
            kind = str(data.get("kind", ""))
            if kind in (interactions.PERMISSION, interactions.AGENT_MODEL):
                record.answer(kind, str(data.get("request_id", "")), str(data.get("value", "")))
        elif event == "cancel":
            agent_id = data.get("agent_id")
            if agent_id:
                record.cancel_agent(str(agent_id))
            else:
                record.cancel("user")
        elif event == "presence":
            record.present()
        elif event == "sync":
            record.present()
            self._spawn(self._sync())

    def _spawn(self, work: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sync(self) -> None:
        """Send the sender's pages the turn's state and its reply so far."""
        await self._flush()
        snapshot = self.record.snapshot()
        text = snapshot.pop("text")
        await self.bus.publish(self.conversation_id, "turn_state", snapshot, to=self.user_guid)
        await self.bus.publish(
            self.conversation_id,
            "turn_text",
            {"stream_id": self.stream_id, "text": text},
            to=self.user_guid,
        )

    async def _flush(self) -> None:
        with self._lock:
            text = "".join(self._text)
            events, self._events, self._text = self._events, [], []
        for event, data, to in events:
            if event == "delta":
                await self._publish_text(str(data.get("text", "")))
                continue
            if event == "tool" and data.get("phase") == "start":
                self.record.text = ""
            payload = data if to is not None else {**data, "stream_id": self.stream_id}
            await self.bus.publish(self.conversation_id, event, payload, to=to)
        if text:
            await self._publish_text(text)

    async def _publish_text(self, text: str) -> None:
        """Publish reply text to every viewer, and keep it as the reply so far."""
        self._seq += 1
        self.record.text += text
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
                if self._server and loop.time() - beat >= HEARTBEAT_SECONDS:
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
        """End the turn: publish what is left, give the lock and the record back, tell the
        viewers. Once; a second call waits until the first has finished."""
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
            self.record.close()
            if self._server:
                await conversation_members.release_turn(
                    self._db, self.conversation_id, self.stream_id
                )
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
            self._finishing = asyncio.ensure_future(self.finish(status_of(fut)))

        future.add_done_callback(done)


# -- Watching a conversation -------------------------------------------------------------------

# Seconds between checks that a watcher still has access (a removal whose notice was lost).
WATCH_RECHECK = 10.0
# Seconds between a watching page's signs of presence while its sender's turn runs.
PRESENCE_EVERY = 10.0


async def _holder(request: Request, user_guid: str, conversation_id: int) -> dict[str, Any] | None:
    """The running turn's holder in a conversation the user may read: ``user_guid``,
    ``stream_id`` and ``started_at``, or None."""
    if is_server():
        row = await conversation_members.current_turn(database(request), user_guid, conversation_id)
        if row is None:
            return None
        return {
            "user_guid": str(row["user_guid"]),
            "stream_id": str(row["stream_id"]),
            "started_at": str(row["started_at"]),
        }
    handle = bus_for(request.app).turn(conversation_id)
    if handle is None:
        return None
    record = interactions.record_for_stream(request.app, handle.stream_id)
    return {
        "user_guid": handle.user_guid,
        "stream_id": handle.stream_id,
        "started_at": record.started_at if record is not None else "",
    }


@router.get("/conversation")
async def watch_conversation(request: Request) -> EventSourceResponse:
    """SSE: follow a conversation the signed-in user may read, live, in both modes.

    Sends ``ready`` (``me``, and the running turn's holder, if any), then the live
    events of :mod:`spark.web.live` as they happen. When the running turn is the
    user's own, the page is a page of its sender: the turn is asked for its state
    (``turn_state`` and ``turn_text`` follow, to this user's pages only), and the
    page's presence is reported while the turn runs, so a held prompt knows a page is
    open. Closes with ``removed`` when the user loses access, and ``deleted`` when the
    conversation is deleted.
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
    holder = await _holder(request, user_guid, conversation_id)
    ready: dict[str, Any] = {"turn": None, "me": user_guid}
    if holder is not None:
        from spark.web.endpoints.conversations import display_names

        guid = holder["user_guid"]
        ready["turn"] = {
            **holder,
            "name": (await display_names(request, [guid]))[guid]["name"],
        }
    server = is_server()

    async def events() -> AsyncGenerator[dict[str, Any], None]:
        loop = asyncio.get_running_loop()
        mine = holder is not None and holder["user_guid"] == user_guid
        try:
            yield {"event": "ready", "data": json.dumps(ready)}
            if mine:
                await bus.publish_control(conversation_id, "sync", {"user_guid": user_guid})
            last_presence = loop.time()
            last_check = loop.time()
            while True:
                timeout = PRESENCE_EVERY if mine else WATCH_RECHECK
                item: dict[str, Any] | None
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
                except TimeoutError:
                    item = None
                now = loop.time()
                if mine and now - last_presence >= PRESENCE_EVERY:
                    last_presence = now
                    await bus.publish_control(conversation_id, "presence", {"user_guid": user_guid})
                if server and (item is None or now - last_check >= WATCH_RECHECK):
                    last_check = now
                    if await conversation_role(db, user_guid, conversation_id) is None:
                        yield {"event": "removed", "data": json.dumps({})}
                        return
                if item is None:
                    continue
                kind = item["event"]
                data = item["data"]
                if kind == "turn_started" and data.get("user_guid") == user_guid:
                    mine = True
                elif kind == "turn_finished" and data.get("user_guid") == user_guid:
                    mine = False
                yield {"event": kind, "data": json.dumps(data)}
                if sub.closed.is_set() and kind in ("removed", "deleted", "closed"):
                    return
        finally:
            bus.unsubscribe(sub)

    return EventSourceResponse(events())
