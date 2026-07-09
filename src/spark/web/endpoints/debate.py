"""Debate Mode endpoints: page, state, prompt, and the SSE stream."""

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

router = APIRouter(prefix="", tags=["debate"])


def _orchestrator(request: Request, status_callback: Any = None):
    """Build a DebateOrchestrator wired to the app's shared services."""
    from spark.core.debate.orchestrator import DebateOrchestrator

    conv_mgr = request.app.state.conversation_manager
    return DebateOrchestrator(
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


@router.get("/debate/{conversation_id}")
async def debate_page(request: Request, conversation_id: int) -> HTMLResponse:
    """Render the three-pane debate page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "debate.html", {"conversation_id": conversation_id})


@router.get("/debate/api/state")
async def debate_state(request: Request, conversation_id: int) -> JSONResponse:
    """Full debate record: config, agents, turns, and exhibits."""
    from spark.database import debates

    db = request.app.state.conversation_manager._db
    cfg = debates.get_debate(db, conversation_id)
    if not cfg:
        return JSONResponse({"error": "Not a debate conversation"}, status_code=404)
    turns = debates.get_turns(db, conversation_id)
    exhibits = debates.get_exhibits(db, conversation_id)
    return JSONResponse(
        {
            "config": cfg,
            "turns": turns,
            "exhibits": {str(k): v for k, v in exhibits.items()},
        }
    )


@router.post("/debate/api/prompt")
async def debate_prompt(request: Request) -> JSONResponse:
    """Queue user input; in QA state, answer immediately via the judge."""
    from spark.database import debates

    data = await request.json()
    cid = data.get("conversation_id")
    message = (data.get("message") or "").strip()
    if not cid or not message:
        return JSONResponse(
            {"error": "conversation_id and message required"}, status_code=400
        )

    db = request.app.state.conversation_manager._db
    cfg = debates.get_debate(db, cid)
    if not cfg:
        return JSONResponse({"error": "Not a debate conversation"}, status_code=404)

    from spark.skills.trigger import resolve_trigger_for_conversation

    message = resolve_trigger_for_conversation(db, _user_guid(request), cid, message)

    orch = _orchestrator(request)
    turn_id = orch.submit_user_prompt(cid, message)
    if cfg["state"] == "qa":
        answer = await asyncio.get_running_loop().run_in_executor(
            None, orch.answer_qa, cid, _user_guid(request), turn_id
        )
        return JSONResponse({"queued": False, "answer": answer})
    return JSONResponse({"queued": True, "turn_id": turn_id})


@router.get("/stream/debate")
async def stream_debate(request: Request, conversation_id: int) -> EventSourceResponse:
    """Drive the debate loop, translating orchestrator events to SSE.

    Mirrors the structure of stream_chat in streaming.py: a worker thread
    runs the orchestrator, a queue carries events, and the poller emits them.
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
                logger.error("Debate stream failed: %s", exc)
                yield {"event": "error", "data": json.dumps({"error": str(exc)})}
            elif turn_token.is_cancelled():
                yield {"event": "cancelled", "data": "{}"}
            else:
                yield {"event": "complete", "data": "{}"}
        finally:
            request.app.state.turn_cancel_tokens.pop(stream_id, None)

    return EventSourceResponse(event_generator())
