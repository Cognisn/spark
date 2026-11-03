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
from spark.identity.access import current_user_guid
from spark.web.db import database, sync_database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["debate"])


def _orchestrator(request: Request, status_callback: Any = None):
    """Build a DebateOrchestrator wired to the app's shared services."""
    from spark.core.debate.orchestrator import DebateOrchestrator
    from spark.identity.access import tools_config_for

    conv_mgr = request.app.state.conversation_manager
    return DebateOrchestrator(
        conv_mgr._db,
        conv_mgr._get_llm_service_for_model,
        # Participants' tools follow the signed-in user's roles (server mode).
        tools_config_for(request, getattr(conv_mgr, "_embedded_tools_config", {}) or {}),
        mcp_manager=getattr(conv_mgr, "_mcp_manager", None),
        mcp_loop=getattr(request.app.state, "_mcp_loop", None),
        status_callback=status_callback,
        tool_permission_callback=getattr(conv_mgr, "_tool_permission_callback", None),
        prompt_caching=getattr(conv_mgr, "_prompt_caching_enabled", True),
    )


@router.get("/debate/{conversation_id}")
async def debate_page(request: Request, conversation_id: int) -> HTMLResponse:
    """Render the three-pane debate page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "debate.html", {"conversation_id": conversation_id})


@router.get("/debate/api/capabilities")
async def debate_capabilities(request: Request) -> JSONResponse:
    """The selectable tool and skill universe for the debate creation wizard."""
    tools_out: list[dict] = []
    try:
        from spark.core.conversation_manager import _TOOL_CATEGORIES
        from spark.identity.access import tools_config_for
        from spark.skills.tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES
        from spark.tools.registry import get_builtin_tools

        conv_mgr = request.app.state.conversation_manager
        config = getattr(conv_mgr, "_embedded_tools_config", {}) or {"embedded_tools": {}}
        config = tools_config_for(request, config)
        offered = {t["name"]: t.get("description", "") for t in get_builtin_tools(config)}

        never = {"spawn_agent", "list_provider_models"}
        never |= set(READ_TOOL_NAMES) | set(WRITE_TOOL_NAMES)
        from spark.core.debate.tools import MEMORY_TOOL_NAMES

        never |= set(MEMORY_TOOL_NAMES)

        for category, names in _TOOL_CATEGORIES.items():
            if category in ("skills", "skill_authoring", "agents", "memory"):
                continue
            entries = [
                {"name": n, "description": offered[n][:80]}
                for n in names
                if n in offered and n not in never
            ]
            if entries:
                tools_out.append({"group": category, "tools": entries})

        mcp_mgr = getattr(request.app.state, "mcp_manager", None)
        if mcp_mgr:
            by_server: dict[str, list[dict]] = {}
            for tool in mcp_mgr._tools_cache or []:
                server = tool.get("server", "mcp")
                by_server.setdefault(server, []).append(
                    {
                        "name": tool.get("name", ""),
                        "description": (tool.get("description") or "")[:80],
                    }
                )
            for server, entries in by_server.items():
                tools_out.append({"group": f"MCP: {server}", "tools": entries})
    except Exception:  # noqa: BLE001 - degrade to whatever resolved
        logger.warning("Capabilities tools listing degraded", exc_info=True)

    skills_out: list[dict] = []
    try:
        from spark.database import skills as skills_db
        from spark.skills.manager import get_skills_manager

        manager = getattr(request.app.state, "skills_manager", None) or get_skills_manager()
        # Only globally-enabled skills are offered; a skill disabled in the global
        # settings never reaches the wizard, matching how tools are gated by their
        # category config. Absent state defaults to enabled.
        states = await skills_db.get_skill_states(database(request), current_user_guid(request))
        skills_out = [
            {"name": s["name"], "description": s["description"][:80]}
            for s in manager.list_skills()
            if states.get(s["name"], True)
        ]
    except Exception:  # noqa: BLE001
        logger.warning("Capabilities skills listing degraded", exc_info=True)

    return JSONResponse({"tools": tools_out, "skills": skills_out})


@router.get("/debate/api/state")
async def debate_state(request: Request, conversation_id: int) -> JSONResponse:
    """Full debate record: config, agents, turns, and exhibits."""
    from spark.database import debates

    db = database(request)
    user_guid = current_user_guid(request)
    cfg = await debates.get_debate(db, user_guid, conversation_id)
    if not cfg:
        return JSONResponse({"error": "Not a debate conversation"}, status_code=404)
    turns = await debates.get_turns(db, user_guid, conversation_id)
    exhibits = await debates.get_exhibits(db, user_guid, conversation_id)
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
        return JSONResponse({"error": "conversation_id and message required"}, status_code=400)

    db = database(request)
    user_guid = current_user_guid(request)
    cfg = await debates.get_debate(db, user_guid, cid)
    if not cfg:
        return JSONResponse({"error": "Not a debate conversation"}, status_code=404)

    from spark.skills.trigger import resolve_trigger_for_conversation

    message = await resolve_trigger_for_conversation(db, user_guid, cid, message)

    orch = _orchestrator(request)
    from spark.usage import budgets
    from spark.web.endpoints.usage import refused_response, request_roles

    roles = request_roles(request)
    if cfg["state"] == "qa":
        # A question in QA is a new request: refused at 100% of the budget, unstored.
        standing = await budgets.status_for(db, user_guid, roles)
        if standing.state == "exceeded":
            return refused_response(budgets.BudgetExceeded(standing))
    turn_id = await asyncio.to_thread(orch.submit_user_prompt, cid, user_guid, message)
    if cfg["state"] == "qa":
        try:
            answer = await asyncio.get_running_loop().run_in_executor(
                None, lambda: orch.answer_qa(cid, user_guid, turn_id, roles=roles)
            )
        except budgets.BudgetExceeded as refused:
            return refused_response(refused)
        return JSONResponse({"queued": False, "answer": answer})
    return JSONResponse({"queued": True, "turn_id": turn_id})


@router.get("/stream/debate", response_model=None)
async def stream_debate(
    request: Request, conversation_id: int
) -> EventSourceResponse | JSONResponse:
    """Drive the debate loop, translating orchestrator events to SSE.

    Mirrors the structure of stream_chat in streaming.py: a worker thread
    runs the orchestrator, a queue carries events, and the poller emits them.
    """
    from spark.database import debates

    user_guid = current_user_guid(request)
    if not await debates.get_debate(database(request), user_guid, conversation_id):
        return JSONResponse({"error": "Not a debate conversation"}, status_code=404)
    stream_id = uuid.uuid4().hex
    turn_token = CancellationToken()
    if not hasattr(request.app.state, "turn_cancel_tokens"):
        request.app.state.turn_cancel_tokens = {}
    request.app.state.turn_cancel_tokens[stream_id] = turn_token

    events: queue.Queue = queue.Queue()
    orch = _orchestrator(request, status_callback=lambda t, d: events.put((t, d)))
    loop = asyncio.get_running_loop()

    from spark.usage.budgets import BudgetExceeded
    from spark.web.endpoints.usage import refused_event, request_roles

    roles = request_roles(request)

    async def event_generator():
        yield {"event": "stream_start", "data": json.dumps({"stream_id": stream_id})}
        run = loop.run_in_executor(
            None,
            lambda: orch.run(conversation_id, user_guid, cancel_token=turn_token, roles=roles),
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
            if isinstance(exc, BudgetExceeded):
                yield refused_event(exc)
            elif exc:
                logger.error("Debate stream failed: %s", exc)
                yield {"event": "error", "data": json.dumps({"error": str(exc)})}
            elif turn_token.is_cancelled():
                yield {"event": "cancelled", "data": "{}"}
            else:
                yield {"event": "complete", "data": "{}"}
        finally:
            request.app.state.turn_cancel_tokens.pop(stream_id, None)

    return EventSourceResponse(event_generator())
