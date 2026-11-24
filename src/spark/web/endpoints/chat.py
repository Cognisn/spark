"""Chat and message endpoints.

Every route that takes a conversation id asks
:func:`spark.web.scope.conversation_access` for what it needs: reading (history,
tool activity, agents, info, export) is open to every member, sending to the owner
and contributors, and settings, tools and links to the owner. Linked conversations
stay private to the owner: a link's target must be the owner's own conversation.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from spark.core.conversation_manager import ConnectionNotAssigned
from spark.core.reasoning import strip_reasoning_blocks
from spark.database import conversation_members
from spark.database.scope import Capability, Role, allows, conversation_role
from spark.identity.access import current_user_guid, request_permissions
from spark.usage.budgets import BudgetExceeded
from spark.web import attachments, interactions
from spark.web import models as offered_models
from spark.web.db import database, sync_database
from spark.web.endpoints.usage import refused_response, request_roles
from spark.web.scope import ConversationAccess, conversation_access, is_server, not_found

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")


# -- Page rendering -----------------------------------------------------------


@router.get("/{conversation_id}", response_class=HTMLResponse)
async def chat_page(request: Request, conversation_id: int) -> HTMLResponse:
    """Render the chat page for a conversation the signed-in user may read."""
    templates = request.app.state.templates
    conv_mgr = getattr(request.app.state, "conversation_manager", None)

    conversation = None
    role = Role.OWNER
    if conv_mgr:
        access = await conversation_access(request, conversation_id, Capability.READ)
        if isinstance(access, ConversationAccess):
            conversation = dict(access.conversation)
            role = access.role
            # Opening it clears the "new" marker of a conversation shared with them.
            if is_server():
                await conversation_members.mark_seen(
                    database(request), access.user_guid, conversation_id
                )

    if not conversation:
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "conversation": {
                    "id": conversation_id,
                    "name": "Unknown",
                    "model_id": "",
                },
            },
        )

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "conversation": conversation,
            # A conversation pinned to a model no longer offered keeps working, with a notice.
            "model_notice": offered_models.notice(request, conversation.get("model_id")),
            # Sharing is a server's: the page shows the Share button to the owner only.
            "sharing": is_server(),
            "my_role": role.value,
        },
    )


# -- Message API --------------------------------------------------------------


@router.get("/{conversation_id}/api/history")
async def get_history(request: Request, conversation_id: int) -> JSONResponse:
    """API: the message history of a conversation the user may read.

    On a server each message carries its sender's name and initials (``sender``): the
    person who wrote a user message, or whose turn an assistant reply answered.
    """
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access

    user_guid = access.user_guid
    messages = await asyncio.to_thread(conv_mgr.get_messages, conversation_id, user_guid)
    # Each reply's reasoning travels beside its message, for the page to show collapsed.
    reasoning = await asyncio.to_thread(conv_mgr.get_reasoning, conversation_id, user_guid)
    senders: dict[str, dict[str, str]] = {}
    if is_server():
        from spark.web.endpoints.conversations import display_names

        senders = await display_names(request, {str(m.get("user_guid") or "") for m in messages})
    history = []
    for m in messages:
        item = {**m, "reasoning": reasoning[m["id"]]} if m.get("id") in reasoning else dict(m)
        sender = senders.get(str(m.get("user_guid") or ""))
        if sender is not None:
            item["sender"] = sender
        history.append(item)
    return JSONResponse(history)


@router.get("/{conversation_id}/api/tool-activity")
async def get_tool_activity(request: Request, conversation_id: int) -> JSONResponse:
    """API: the tool call history of a conversation the user may read."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access

    from spark.database import mcp_ops

    transactions = await mcp_ops.get_transactions(
        database(request), current_user_guid(request), conversation_id
    )
    # Return newest-last so the panel renders in chronological order.
    transactions.reverse()
    # Strip the embedding-sized binary blob; keep only display fields.
    result = []
    for t in transactions:
        result.append(
            {
                "id": t.get("id"),
                "tool_name": t.get("tool_name", ""),
                "tool_input": t.get("tool_input", "{}"),
                "tool_response": t.get("tool_response", ""),
                "is_error": bool(t.get("is_error")),
                "execution_time_ms": t.get("execution_time_ms"),
                "timestamp": t.get("transaction_timestamp"),
            }
        )
    return JSONResponse(result)


@router.get("/{conversation_id}/api/agent-history")
async def get_agent_history(request: Request, conversation_id: int) -> JSONResponse:
    """API: the agent run history of a conversation the user may read."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access

    from spark.database import agents as agent_db

    runs = await agent_db.get_agent_runs(
        database(request), current_user_guid(request), conversation_id
    )
    # Serialise datetime and JSON fields for the frontend.
    result = []
    for r in runs:
        entry = dict(r)
        # Ensure datetimes are strings
        for key in ("created_at", "completed_at"):
            val = entry.get(key)
            if val and not isinstance(val, str):
                entry[key] = str(val)
        result.append(entry)
    return JSONResponse(result)


@router.post("/{conversation_id}/api/send")
async def send_message(request: Request, conversation_id: int) -> JSONResponse:
    """API: send a message (non-streaming fallback), as the signed-in user's turn.

    Refused (and audited) for a viewer (403), for a sender who may not use the
    conversation's model (403) and while another participant's turn runs (409).
    """
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.CONTRIBUTE)
    if not isinstance(access, ConversationAccess):
        if access.status_code == 403:
            await refuse_send(request, conversation_id, "role")
        return access
    data = await request.json()
    message = data.get("message", "").strip()
    raw_attachments = data.get("attachments")
    if not message and not raw_attachments:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    user_guid = current_user_guid(request)
    attached = await attachments.attach(request, user_guid, raw_attachments, message)
    if isinstance(attached, JSONResponse):
        return attached

    # The turn's tools follow the signed-in user's roles (server mode), and so does its
    # token budget, exactly as on the stream.
    permissions = request_permissions(request)
    roles = request_roles(request)
    from spark.core.cancellation import CancellationToken
    from spark.web.endpoints.streaming import LocalTurn, RunningTurn, TurnRefused

    try:
        # One turn at a time (CS29): the lock is given back when the turn ends, even if
        # this request is gone by then. Personal mode's turns run as before.
        turn: RunningTurn | LocalTurn = (
            await RunningTurn.start(request, conversation_id, user_guid)
            if is_server()
            else LocalTurn(CancellationToken())
        )
        work = asyncio.get_running_loop().run_in_executor(
            None,
            lambda: conv_mgr.send_message(
                conversation_id,
                attached,
                user_guid,
                permissions=permissions,
                roles=roles,
                cancel_token=turn.token,
            ),
        )
        turn.finish_after(work)
        try:
            result = await asyncio.shield(work)
        finally:
            # The turn is over when its work is: give the lock back before answering
            # (a request that went away leaves it to the work's own callback).
            if work.done():
                await turn.finish(_status_of(work))
        return JSONResponse(result)
    except TurnRefused as busy:
        await refuse_send(request, conversation_id, "turn")
        return JSONResponse({"error": str(busy), "holder": busy.holder}, status_code=409)
    except BudgetExceeded as refused:
        # Refused at turn start: nothing of the turn was stored or sent to the model.
        return refused_response(refused)
    except ConnectionNotAssigned as refused:
        # The conversation's connection is not (or no longer) assigned to the user (M9).
        await refuse_send(request, conversation_id, "model")
        return JSONResponse({"error": str(refused)}, status_code=403)
    except Exception as e:
        logger.error("Send message failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


def _status_of(work: asyncio.Future[Any]) -> str:
    if work.cancelled():
        return "cancelled"
    if work.exception() is not None:
        return "failed"
    result = work.result()
    return str(result.get("status", "ok")) if isinstance(result, dict) else "ok"


async def refuse_send(request: Request, conversation_id: int, reason: str) -> None:
    """Audit a refused send: ``role`` (a viewer), ``model`` (a model the sender may not use)
    or ``turn`` (another participant's turn is running)."""
    from spark.identity import audit

    await audit.emit_request(
        request,
        audit.CONVERSATION_SEND_REFUSED,
        "failure",
        resource_type="conversation",
        resource_id=str(conversation_id),
        details={"reason": reason},
    )


async def _sharing_info(
    request: Request, access: ConversationAccess, conv_mgr: Any, model_id: str
) -> dict[str, Any]:
    """The signed-in user's place in the conversation, for the page."""
    db = database(request)
    conversation_id = int(access.conversation["id"])
    refusal: str | None = None
    if not allows(access.role, Capability.CONTRIBUTE):
        refusal = "You have read-only access to this conversation."
    elif is_server():
        refusal = await asyncio.to_thread(
            conv_mgr.model_refusal,
            model_id,
            access.user_guid,
            request_roles(request),
            request_permissions(request),
        )
    info: dict[str, Any] = {
        "my_role": access.role.value,
        "may_send": refusal is None,
        "send_refusal": refusal,
        "may_manage": allows(access.role, Capability.MANAGE),
        "sharing": is_server(),
        "shared": False,
        "members": [],
        "turn": None,
    }
    if not is_server():
        return info
    from spark.web.endpoints.conversations import display_names, members_view

    members = await members_view(request, access)
    info["members"] = members
    info["shared"] = len(members) > 1
    turn = await conversation_members.current_turn(db, access.user_guid, conversation_id)
    if turn is not None:
        holder = str(turn["user_guid"])
        names = await display_names(request, [holder])
        info["turn"] = {"user_guid": holder, "name": names[holder]["name"]}
    return info


# -- Info & Settings ----------------------------------------------------------


@router.get("/{conversation_id}/api/info")
async def get_info(request: Request, conversation_id: int) -> JSONResponse:
    """API: conversation info and settings, with the signed-in user's place in it.

    ``my_role``, ``may_send`` (and ``send_refusal``, the reason when they may not: a
    read-only role, or a model they may not use), ``may_manage``, ``shared``, the
    ``members`` (server mode) and the running turn's holder (``turn``).
    """
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid
    conv = dict(access.conversation)

    model_id = conv.get("model_id", "")
    # The model's context window, from the connections' catalogue.
    context_window = offered_models.context_window(request, model_id)

    # Sum token usage from agent runs for this conversation
    agent_input_tokens = 0
    agent_output_tokens = 0
    try:
        from spark.database import agents as agent_db

        runs = await agent_db.get_agent_runs(database(request), user_guid, conversation_id)
        for r in runs:
            agent_input_tokens += r.get("input_tokens", 0) or 0
            agent_output_tokens += r.get("output_tokens", 0) or 0
    except Exception:
        pass

    sharing = await _sharing_info(request, access, conv_mgr, model_id)
    return JSONResponse(
        {
            **sharing,
            "id": conv.get("id"),
            "name": conv.get("name"),
            "model_id": model_id,
            # False (with model_notice) once the model is no longer offered to the user:
            # the page says so.
            "model_offered": offered_models.is_offered(request, model_id) if model_id else True,
            "model_notice": offered_models.notice(request, model_id),
            "created_at": conv.get("created_at"),
            "tokens_sent": (conv.get("tokens_sent", 0) or 0) + agent_input_tokens,
            "tokens_received": (conv.get("tokens_received", 0) or 0) + agent_output_tokens,
            "total_tokens": (conv.get("total_tokens", 0) or 0)
            + agent_input_tokens
            + agent_output_tokens,
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
            "prompt_caching": bool(conv.get("prompt_caching", True)),
            "agents_enabled": bool(conv.get("agents_enabled", False)),
            "agent_mode": conv.get("agent_mode") or "",
            "agent_model_selection": conv.get("agent_model_selection") or "",
            "kg_local_enabled": bool(conv.get("kg_local_enabled", False)),
            "kg_use_global": conv.get("kg_use_global", 1) != 0,
            "kg_auto_context": conv.get("kg_auto_context", 1) != 0,
        }
    )


@router.post("/{conversation_id}/api/settings")
async def update_settings(request: Request, conversation_id: int) -> JSONResponse:
    """API: update per-conversation settings (the owner only)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid
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
        "prompt_caching",
        "agents_enabled",
        "agent_mode",
        "agent_model_selection",
        "kg_local_enabled",
        "kg_use_global",
        "kg_auto_context",
    }
    _BOOL_FIELDS = {
        "memory_enabled",
        "rag_enabled",
        "rag_tool_enabled",
        "include_tool_results",
        "prompt_caching",
        "agents_enabled",
        "kg_local_enabled",
        "kg_use_global",
        "kg_auto_context",
    }

    updates: dict[str, Any] = {}
    for key in _ALLOWED_FIELDS:
        if key in data:
            val = data[key]
            if key in _BOOL_FIELDS:
                val = int(bool(val))
            updates[key] = val

    try:
        await conversations.update_conversation(
            database(request), conversation_id, user_guid, **updates
        )
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.error("Failed to update conversation settings: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


# -- Tools per conversation ---------------------------------------------------


@router.get("/{conversation_id}/api/tools")
async def get_tools(request: Request, conversation_id: int) -> JSONResponse:
    """API: get tool enable/disable states for this conversation (any member reads them)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"embedded": [], "mcp_servers": []})

    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid

    from spark.database import mcp_ops
    from spark.identity.access import tools_config_for
    from spark.tools.registry import get_builtin_tools

    # Embedded tools: those the signed-in user's roles allow
    all_builtin = get_builtin_tools(tools_config_for(request, conv_mgr._embedded_tools_config))
    embedded = []
    for t in all_builtin:
        enabled = await mcp_ops.is_embedded_tool_enabled(
            database(request), user_guid, conversation_id, t["name"]
        )
        embedded.append(
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "enabled": enabled,
            }
        )

    # MCP servers
    mcp_servers: list[dict] = []
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr:
        from spark.identity.access import request_permissions

        # Only the servers granted to this user (server mode, A12).
        granted = request_permissions(request)
        tools_cache = mcp_mgr.cached_tools(permissions=granted)
        for server_name, client in mcp_mgr.servers_for(granted).items():
            if not client.connected:
                continue
            server_enabled = await mcp_ops.is_mcp_server_enabled(
                database(request), user_guid, conversation_id, server_name
            )
            server_tools = []
            for t in tools_cache:
                if t.get("server") == server_name:
                    tool_enabled = await mcp_ops.is_embedded_tool_enabled(
                        database(request), user_guid, conversation_id, t["name"]
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

    # Skills (global enable state folded with per-conversation overrides)
    skills_out: list[dict] = []
    try:
        from spark.database import skills as skills_db
        from spark.skills.manager import get_skills_manager

        manager = getattr(request.app.state, "skills_manager", None) or get_skills_manager()
        global_states = await skills_db.get_skill_states(database(request), user_guid)
        conv_states = await skills_db.get_conversation_skill_states(
            database(request), user_guid, conversation_id
        )
        for skill in manager.list_skills():
            enabled_flag = global_states.get(skill["name"], True) and conv_states.get(
                skill["name"], True
            )
            skills_out.append(
                {
                    "name": skill["name"],
                    "description": skill["description"],
                    "enabled": enabled_flag,
                }
            )
    except Exception:  # noqa: BLE001 - skills must never break the tools panel
        logger.warning("Skills unavailable for tools panel", exc_info=True)

    return JSONResponse({"embedded": embedded, "mcp_servers": mcp_servers, "skills": skills_out})


@router.post("/{conversation_id}/api/tools")
async def toggle_tool(request: Request, conversation_id: int) -> JSONResponse:
    """API: toggle a tool or MCP server for this conversation (the owner only)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid
    data = await request.json()
    tool_type = data.get("type", "embedded")  # embedded | mcp_server
    name = data.get("name", "")
    enabled = data.get("enabled", True)

    from spark.database import mcp_ops

    if tool_type == "embedded":
        await mcp_ops.set_embedded_tool_enabled(
            database(request), conversation_id, name, enabled, user_guid
        )
    elif tool_type == "mcp_server":
        await mcp_ops.set_mcp_server_enabled(
            database(request), conversation_id, name, enabled, user_guid
        )
    elif tool_type == "skill":
        from spark.database import skills as skills_db

        await skills_db.set_conversation_skill_enabled(
            database(request), user_guid, conversation_id, name, enabled
        )

    return JSONResponse({"status": "ok"})


# -- Linked Conversations ----------------------------------------------------


@router.get("/{conversation_id}/api/links")
async def get_links(request: Request, conversation_id: int) -> JSONResponse:
    """API: get linked conversations (the owner's own, private to them)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"linked": [], "available": []})

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid

    from spark.database import conversation_links

    linked = await conversation_links.get_links(database(request), conversation_id, user_guid)
    available = await conversation_links.get_linkable_conversations(
        database(request), conversation_id, user_guid
    )

    return JSONResponse({"linked": linked, "available": available})


@router.post("/{conversation_id}/api/links")
async def add_link(request: Request, conversation_id: int) -> JSONResponse:
    """API: link to another of the owner's own conversations."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid
    data = await request.json()
    target_id = data.get("target_id")
    if not target_id:
        return JSONResponse({"error": "target_id required"}, status_code=400)
    # A link's target is the owner's own: a conversation shared with them is not linkable.
    if (
        not isinstance(target_id, int)
        or await conversation_role(database(request), user_guid, target_id) is not Role.OWNER
    ):
        return not_found()

    from spark.database import conversation_links

    success = await conversation_links.add_link(
        database(request), conversation_id, target_id, user_guid
    )
    if success:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"error": "Link already exists or invalid"}, status_code=400)


@router.delete("/{conversation_id}/api/links/{target_id}")
async def remove_link(request: Request, conversation_id: int, target_id: int) -> JSONResponse:
    """API: remove a conversation link (the owner only)."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    access = await conversation_access(request, conversation_id, Capability.MANAGE)
    if not isinstance(access, ConversationAccess):
        return access
    user_guid = access.user_guid

    from spark.database import conversation_links

    await conversation_links.remove_link(database(request), conversation_id, target_id, user_guid)
    return JSONResponse({"status": "ok"})


# -- Export -------------------------------------------------------------------


@router.get("/{conversation_id}/api/export")
async def export_conversation(request: Request, conversation_id: int):  # type: ignore[no-untyped-def]
    """API: export conversation as a file download (any member; audited in server mode)."""
    access = await conversation_access(request, conversation_id, Capability.READ)
    if not isinstance(access, ConversationAccess):
        return access
    response = await _export_response(request, access)
    if response.status_code == 200:
        from spark.identity import audit

        await audit.emit_request(
            request,
            audit.EXPORT_DOWNLOADED,
            resource_type="conversation",
            resource_id=str(conversation_id),
            details={
                "kind": "conversation",
                "format": request.query_params.get("format", "markdown"),
            },
        )
    return response


async def _export_response(request: Request, access: ConversationAccess):  # type: ignore[no-untyped-def]
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    fmt = request.query_params.get("format", "markdown")
    conversation_id = int(access.conversation["id"])
    user_guid = access.user_guid
    conv = dict(access.conversation)

    msgs = await asyncio.to_thread(conv_mgr.get_messages, conversation_id, user_guid)
    # A stored tool-use message carries the model's reasoning as provider state: an
    # export holds what was said, not what the model thought.
    msgs = [{**m, "content": strip_reasoning_blocks(m.get("content", ""))} for m in msgs]
    name = conv.get("name", "conversation")

    # Debates and panels export from their structured turn record, not messages.
    if conv.get("conversation_type") in ("debate", "panel"):
        from spark.core.debate.export import export_debate_json, export_debate_markdown

        if fmt == "markdown":
            content = await asyncio.to_thread(
                export_debate_markdown, sync_database(request), user_guid, conversation_id
            )
            return StreamingResponse(
                iter([content]),
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
            )
        if fmt == "json":
            content = await asyncio.to_thread(
                export_debate_json, sync_database(request), user_guid, conversation_id
            )
            return StreamingResponse(
                iter([content]),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
            )
        if fmt == "html":
            from spark.core.debate.export import export_debate_html

            content = await asyncio.to_thread(
                export_debate_html, sync_database(request), user_guid, conversation_id
            )
            return StreamingResponse(
                iter([content]),
                media_type="text/html",
                headers={"Content-Disposition": f'attachment; filename="{name}.html"'},
            )
        return JSONResponse(
            {"error": "Format not supported for this conversation type"}, status_code=400
        )

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


@router.post("/agent/model-approve")
async def approve_agent_model(request: Request) -> JSONResponse:
    """API: respond to an agent model approval request.

    Signals the streaming thread that is waiting for the user to approve or
    override the model selected for a sub-agent.
    """
    data = await request.json()
    request_id = data.get("request_id")
    model_id = data.get("model_id", "")
    if not interactions.belongs_to_caller(request, interactions.AGENT_MODEL, request_id):
        return not_found()
    if refused := offered_models.not_offered(request, model_id):
        return refused

    events = getattr(request.app.state, "agent_model_events", {})
    responses = getattr(request.app.state, "agent_model_responses", {})

    if request_id in events:
        responses[request_id] = model_id
        events[request_id].set()
        logger.info("Agent model approval for %s: %s", request_id, model_id)
    else:
        logger.warning("Agent model approval for unknown request %s", request_id)

    return JSONResponse({"status": "ok"})


@router.post("/permission/respond")
async def permission_respond(request: Request) -> JSONResponse:
    """API: respond to a tool permission request.

    Signals the streaming thread that's waiting for this permission decision.
    """
    data = await request.json()
    request_id = data.get("request_id")
    decision = data.get("decision", "denied")  # once | allowed | denied
    if not interactions.belongs_to_caller(request, interactions.PERMISSION, request_id):
        return not_found()

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


@router.post("/agent/cancel")
async def cancel_agent(request: Request) -> JSONResponse:
    """API: cancel a running sub-agent.

    Idempotent in personal mode: unknown ids return 200. A server answers 404
    for an id that is not the signed-in user's live agent.
    """
    data = await request.json()
    agent_id = data.get("agent_id", "")
    if not interactions.belongs_to_caller(request, interactions.AGENT, agent_id):
        return not_found()
    tokens = getattr(request.app.state, "agent_cancel_tokens", {})
    tok = tokens.get(agent_id)
    if tok is not None:
        tok.cancel("user")
        logger.info("Agent cancel requested for %s", agent_id)
    else:
        logger.info("Agent cancel for unknown id %s — ignored", agent_id)
    return JSONResponse({"status": "ok"})
