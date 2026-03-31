"""Chat and message endpoints."""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")


# -- Page rendering -----------------------------------------------------------


@router.get("/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: int) -> HTMLResponse:
    """Render the chat page for a conversation."""
    templates = request.app.state.templates
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    user_guid = _get_user_guid(request)

    conversation = None
    if conv_mgr:
        conversation = conv_mgr.get_conversation(conversation_id, user_guid)

    if not conversation:
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "conversation": {"id": conversation_id, "name": "Unknown", "model_id": ""},
            },
        )

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "conversation": conversation,
        },
    )


# -- Message API --------------------------------------------------------------


@router.get("/{conversation_id}/api/history")
async def get_history(request: Request, conversation_id: int) -> JSONResponse:
    """API: get message history for a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])

    messages = conv_mgr.get_messages(conversation_id)
    return JSONResponse(messages)


@router.post("/{conversation_id}/api/send")
async def send_message(request: Request, conversation_id: int) -> JSONResponse:
    """API: send a message (non-streaming fallback)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    message = data.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    user_guid = _get_user_guid(request)

    try:
        result = conv_mgr.send_message(conversation_id, message, user_guid)
        return JSONResponse(result)
    except Exception as e:
        logger.error("Send message failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# -- Info & Settings ----------------------------------------------------------


@router.get("/{conversation_id}/api/info")
async def get_info(request: Request, conversation_id: int) -> JSONResponse:
    """API: get conversation info and settings."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)
    conv = conv_mgr.get_conversation(conversation_id, user_guid)
    if not conv:
        return JSONResponse({"error": "Not found"}, status_code=404)

    from spark.llm.context_limits import ContextLimitResolver

    resolver = ContextLimitResolver()
    model_id = conv.get("model_id", "")
    context_window = resolver.get_context_window(model_id)

    return JSONResponse(
        {
            "id": conv.get("id"),
            "name": conv.get("name"),
            "model_id": model_id,
            "created_at": conv.get("created_at"),
            "tokens_sent": conv.get("tokens_sent", 0),
            "tokens_received": conv.get("tokens_received", 0),
            "total_tokens": conv.get("total_tokens", 0),
            "context_window": context_window,
            "instructions": conv.get("instructions"),
            "compaction_threshold": conv.get("compaction_threshold"),
            "compaction_summary_ratio": conv.get("compaction_summary_ratio"),
            "memory_enabled": bool(conv.get("memory_enabled", True)),
            "rag_enabled": bool(conv.get("rag_enabled", True)),
            "rag_top_k": conv.get("rag_top_k", 5),
            "rag_threshold": conv.get("rag_threshold", 0.4),
            "rag_tool_enabled": bool(conv.get("rag_tool_enabled", False)),
            "max_history_messages": conv.get("max_history_messages"),
            "include_tool_results": bool(conv.get("include_tool_results", True)),
        }
    )


@router.post("/{conversation_id}/api/settings")
async def update_settings(request: Request, conversation_id: int) -> JSONResponse:
    """API: update per-conversation settings."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)
    data = await request.json()

    from spark.database import conversations

    _ALLOWED_FIELDS = {
        "instructions",
        "compaction_threshold",
        "compaction_summary_ratio",
        "memory_enabled",
        "rag_enabled",
        "rag_top_k",
        "rag_threshold",
        "rag_tool_enabled",
        "max_history_messages",
        "include_tool_results",
    }
    _BOOL_FIELDS = {
        "memory_enabled",
        "rag_enabled",
        "rag_tool_enabled",
        "include_tool_results",
    }

    updates: dict[str, Any] = {}
    for key in _ALLOWED_FIELDS:
        if key in data:
            val = data[key]
            if key in _BOOL_FIELDS:
                val = int(bool(val))
            updates[key] = val

    try:
        conversations.update_conversation(conv_mgr._db, conversation_id, user_guid, **updates)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.error("Failed to update conversation settings: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


# -- Tools per conversation ---------------------------------------------------


@router.get("/{conversation_id}/api/tools")
async def get_tools(request: Request, conversation_id: int) -> JSONResponse:
    """API: get tool enable/disable states for this conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"embedded": [], "mcp_servers": []})

    user_guid = _get_user_guid(request)

    from spark.database import mcp_ops
    from spark.tools.registry import get_builtin_tools

    # Embedded tools
    all_builtin = get_builtin_tools(conv_mgr._embedded_tools_config)
    embedded = []
    for t in all_builtin:
        enabled = mcp_ops.is_embedded_tool_enabled(conv_mgr._db, conversation_id, t["name"])
        embedded.append(
            {"name": t["name"], "description": t.get("description", ""), "enabled": enabled}
        )

    # MCP servers
    mcp_servers: list[dict] = []
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr:
        tools_cache = mcp_mgr._tools_cache or []
        for server_name, client in mcp_mgr.servers.items():
            if not client.connected:
                continue
            server_enabled = mcp_ops.is_mcp_server_enabled(
                conv_mgr._db, conversation_id, server_name
            )
            server_tools = []
            for t in tools_cache:
                if t.get("server") == server_name:
                    tool_enabled = mcp_ops.is_embedded_tool_enabled(
                        conv_mgr._db, conversation_id, t["name"]
                    )
                    server_tools.append(
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "enabled": tool_enabled,
                        }
                    )
            mcp_servers.append(
                {
                    "name": server_name,
                    "enabled": server_enabled,
                    "tools": server_tools,
                }
            )

    return JSONResponse({"embedded": embedded, "mcp_servers": mcp_servers})


@router.post("/{conversation_id}/api/tools")
async def toggle_tool(request: Request, conversation_id: int) -> JSONResponse:
    """API: toggle a tool or MCP server for this conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)
    data = await request.json()
    tool_type = data.get("type", "embedded")  # embedded | mcp_server
    name = data.get("name", "")
    enabled = data.get("enabled", True)

    from spark.database import mcp_ops

    if tool_type == "embedded":
        mcp_ops.set_embedded_tool_enabled(conv_mgr._db, conversation_id, name, enabled, user_guid)
    elif tool_type == "mcp_server":
        mcp_ops.set_mcp_server_enabled(conv_mgr._db, conversation_id, name, enabled, user_guid)

    return JSONResponse({"status": "ok"})


# -- Linked Conversations ----------------------------------------------------


@router.get("/{conversation_id}/api/links")
async def get_links(request: Request, conversation_id: int) -> JSONResponse:
    """API: get linked conversations."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"linked": [], "available": []})

    user_guid = _get_user_guid(request)

    from spark.database import conversation_links

    linked = conversation_links.get_links(conv_mgr._db, conversation_id, user_guid)
    available = conversation_links.get_linkable_conversations(
        conv_mgr._db, conversation_id, user_guid
    )

    return JSONResponse({"linked": linked, "available": available})


@router.post("/{conversation_id}/api/links")
async def add_link(request: Request, conversation_id: int) -> JSONResponse:
    """API: link to another conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)
    data = await request.json()
    target_id = data.get("target_id")
    if not target_id:
        return JSONResponse({"error": "target_id required"}, status_code=400)

    from spark.database import conversation_links

    success = conversation_links.add_link(conv_mgr._db, conversation_id, target_id, user_guid)
    if success:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Link already exists or invalid"}, status_code=400)


@router.delete("/{conversation_id}/api/links/{target_id}")
async def remove_link(request: Request, conversation_id: int, target_id: int) -> JSONResponse:
    """API: remove a conversation link."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)

    from spark.database import conversation_links

    conversation_links.remove_link(conv_mgr._db, conversation_id, target_id, user_guid)
    return JSONResponse({"status": "ok"})


# -- Export -------------------------------------------------------------------


@router.get("/{conversation_id}/api/export")
async def export_conversation(request: Request, conversation_id: int):  # type: ignore[no-untyped-def]
    """API: export conversation as a file download."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)
    fmt = request.query_params.get("format", "markdown")

    conv = conv_mgr.get_conversation(conversation_id, user_guid)
    if not conv:
        return JSONResponse({"error": "Not found"}, status_code=404)

    msgs = conv_mgr.get_messages(conversation_id)
    name = conv.get("name", "conversation")

    if fmt == "json":
        content = json.dumps({"conversation": conv, "messages": msgs}, indent=2, default=str)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
        )

    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "role", "content"])
        for m in msgs:
            writer.writerow([m.get("timestamp", ""), m.get("role", ""), m.get("content", "")])
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )

    elif fmt == "html":
        lines = [f"<html><head><title>{name}</title></head><body>"]
        lines.append(f"<h1>{name}</h1>")
        for m in msgs:
            role = m.get("role", "user")
            content = m.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f'<div class="{role}"><strong>{role}:</strong><p>{content}</p></div>')
        lines.append("</body></html>")
        return StreamingResponse(
            iter(["\n".join(lines)]),
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="{name}.html"'},
        )

    else:  # markdown
        lines = [f"# {name}\n"]
        for m in msgs:
            role = m.get("role", "user").capitalize()
            content = m.get("content", "")
            lines.append(f"## {role}\n\n{content}\n")
        return StreamingResponse(
            iter(["\n".join(lines)]),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
        )


# -- Permission ---------------------------------------------------------------


@router.post("/permission/respond")
async def permission_respond(request: Request) -> JSONResponse:
    """API: respond to a tool permission request.

    Signals the streaming thread that's waiting for this permission decision.
    """
    data = await request.json()
    request_id = data.get("request_id")
    decision = data.get("decision", "denied")  # once | allowed | denied

    # Signal the waiting thread
    events = getattr(request.app.state, "permission_events", {})
    responses = getattr(request.app.state, "permission_responses", {})

    if request_id in events:
        responses[request_id] = decision
        events[request_id].set()  # Unblock the waiting thread
        logger.info("Permission response for %s: %s", request_id, decision)
    else:
        logger.warning("Permission response for unknown request %s", request_id)

    return JSONResponse({"status": "ok"})


def _get_user_guid(request: Request) -> str:
    return getattr(request.app.state, "user_guid", "default")
