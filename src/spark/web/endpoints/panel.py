"""Panel endpoints: page, state, state-aware prompt, and the SSE stream."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from spark.core.cancellation import CancellationToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["panel"])


def _orchestrator(request: Request, status_callback: Any = None):
    """Build a PanelOrchestrator wired to the app's shared services."""
    from spark.core.panel.orchestrator import PanelOrchestrator

    conv_mgr = request.app.state.conversation_manager
    return PanelOrchestrator(
        conv_mgr._db,
        conv_mgr._get_llm_service_for_model,
        getattr(conv_mgr, "_embedded_tools_config", {}) or {},
        mcp_manager=getattr(conv_mgr, "_mcp_manager", None),
        mcp_loop=getattr(request.app.state, "_mcp_loop", None),
        status_callback=status_callback,
        tool_permission_callback=getattr(conv_mgr, "_tool_permission_callback", None),
    )


def _user_guid(request: Request) -> str:
    return getattr(request.app.state, "user_guid", "default")


def _panel_config(request: Request, conversation_id: int) -> dict | None:
    """The panel config, or None when the conversation is not a panel."""
    from spark.database import debates

    db = request.app.state.conversation_manager._db
    ph = db.placeholder
    cur = db.execute(
        f"SELECT conversation_type FROM conversations WHERE id = {ph}",
        (conversation_id,),
    )
    row = cur.fetchone()
    if not row or row[0] != "panel":
        return None
    return debates.get_debate(db, conversation_id)


@router.get("/panel/{conversation_id}")
async def panel_page(request: Request, conversation_id: int) -> HTMLResponse:
    """Render the threaded panel page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "panel.html", {"conversation_id": conversation_id}
    )


@router.get("/panel/api/state")
async def panel_state(request: Request, conversation_id: int) -> JSONResponse:
    """Full panel record: config, agents, turns, and exhibits."""
    from spark.database import debates

    cfg = _panel_config(request, conversation_id)
    if not cfg:
        return JSONResponse({"error": "Not a panel conversation"}, status_code=404)
    db = request.app.state.conversation_manager._db
    turns = debates.get_turns(db, conversation_id)
    exhibits = debates.get_exhibits(db, conversation_id)
    orch = _orchestrator(request)
    return JSONResponse(
        {
            "config": cfg,
            "turns": turns,
            "exhibits": {str(k): v for k, v in exhibits.items()},
            "awaiting_human": orch.awaiting_human(conversation_id, _user_guid(request)),
        }
    )


@router.post("/panel/api/prompt")
async def panel_prompt(request: Request) -> JSONResponse:
    """Route user input by panel state.

    During a human panellist's turn the message is their contribution; in QA
    it is answered synchronously by the moderator; otherwise it queues as a
    directive turn.
    """
    data = await request.json()
    cid = data.get("conversation_id")
    message = (data.get("message") or "").strip()
    if not cid or not message:
        return JSONResponse(
            {"error": "conversation_id and message required"}, status_code=400
        )

    cfg = _panel_config(request, cid)
    if not cfg:
        return JSONResponse({"error": "Not a panel conversation"}, status_code=404)

    orch = _orchestrator(request)
    user_guid = _user_guid(request)

    pending = orch.awaiting_human(cid, user_guid)
    if pending:
        turn_id = orch.submit_contribution(cid, user_guid, message)
        return JSONResponse({"accepted": "contribution", "turn_id": turn_id})

    from spark.skills.trigger import resolve_trigger_for_conversation

    db = request.app.state.conversation_manager._db
    message = resolve_trigger_for_conversation(db, user_guid, cid, message)

    turn_id = orch.submit_user_prompt(cid, message)
    if cfg["state"] == "qa":
        answer = await asyncio.get_running_loop().run_in_executor(
            None, orch.answer_qa, cid, user_guid, turn_id
        )
        return JSONResponse({"queued": False, "answer": answer})
    return JSONResponse({"queued": True, "turn_id": turn_id})


@router.get("/stream/panel")
async def stream_panel(request: Request, conversation_id: int) -> EventSourceResponse:
    """Drive the panel loop, translating orchestrator events to SSE.

    Mirrors stream_debate: a worker thread runs the orchestrator, a queue
    carries events, and the poller emits them. A human turn ends the run
    with a human_turn event already emitted; the client posts the
    contribution then reconnects to resume.
    """
    stream_id = uuid.uuid4().hex
    turn_token = CancellationToken()
    if not hasattr(request.app.state, "turn_cancel_tokens"):
        request.app.state.turn_cancel_tokens = {}
    request.app.state.turn_cancel_tokens[stream_id] = turn_token

    events: queue.Queue = queue.Queue()
    orch = _orchestrator(request, status_callback=lambda t, d: events.put((t, d)))
    user_guid = _user_guid(request)
    loop = asyncio.get_running_loop()

    async def event_generator():
        yield {"event": "stream_start", "data": json.dumps({"stream_id": stream_id})}
        run = loop.run_in_executor(
            None,
            lambda: orch.run(conversation_id, user_guid, cancel_token=turn_token),
        )
        try:
            while True:
                try:
                    etype, data = events.get_nowait()
                    yield {"event": etype, "data": json.dumps(data)}
                except queue.Empty:
                    if run.done():
                        break
                    await asyncio.sleep(0.2)
            # Drain any remaining events after the run finishes
            while not events.empty():
                etype, data = events.get_nowait()
                yield {"event": etype, "data": json.dumps(data)}
            exc = run.exception()
            if exc:
                logger.error("Panel stream failed: %s", exc)
                yield {"event": "error", "data": json.dumps({"error": str(exc)})}
            elif turn_token.is_cancelled():
                yield {"event": "cancelled", "data": "{}"}
            else:
                yield {"event": "complete", "data": "{}"}
        finally:
            request.app.state.turn_cancel_tokens.pop(stream_id, None)

    return EventSourceResponse(event_generator())
