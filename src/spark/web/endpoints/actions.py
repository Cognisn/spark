"""Autonomous action management endpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from spark.identity.access import current_user_guid, require_permission
from spark.identity.permissions import ACTIONS
from spark.web.db import database, sync_database
from spark.web.models import not_offered, offered_models
from spark.web.scope import not_found, owned_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions")

# Creating, changing and running actions needs spark.actions (a power user's);
# listing, reading and deleting one's own actions stay open to every user.
# Personal mode's local operator holds every permission.
_MAY_RUN_ACTIONS = [Depends(require_permission(ACTIONS))]


def _actions_enabled(request: Request) -> bool:
    """Check if autonomous actions are enabled in settings."""
    ctx = getattr(request.app.state, "ctx", None)
    if not ctx:
        return False
    return bool(ctx.settings.get("autonomous_actions.enabled", False))


@router.get("", response_class=HTMLResponse)
async def actions_page(request: Request):  # type: ignore[no-untyped-def]
    if not _actions_enabled(request):
        return RedirectResponse(url="/", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "actions.html")


def _guard(request: Request) -> JSONResponse | None:
    """Return a 403 response if actions are disabled, otherwise None."""
    if not _actions_enabled(request):
        return JSONResponse({"error": "Autonomous actions are disabled"}, status_code=403)
    return None


@router.get("/api/list")
async def list_actions(request: Request) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse([])
    from spark.database import autonomous_actions

    # The user's enabled and disabled ones
    all_actions = await autonomous_actions.get_all_actions(db, current_user_guid(request))
    return JSONResponse(all_actions)


@router.post("/api/create", dependencies=_MAY_RUN_ACTIONS)
async def create_action(request: Request) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    if refused := not_offered(request, data.get("model_id")):
        return refused
    from spark.database import autonomous_actions

    try:
        aid = await autonomous_actions.create_action(
            db,
            name=data["name"],
            action_prompt=data["prompt"],
            model_id=data["model_id"],
            user_guid=current_user_guid(request),
            description=data.get("description"),
            schedule_type=data.get("schedule_type", "one_off"),
            schedule_config=data.get("schedule_config"),
            context_mode=data.get("context_mode", "fresh"),
            max_failures=int(data.get("max_failures", 3)),
            max_tokens=int(data.get("max_tokens", 8192)),
        )
        return JSONResponse({"status": "ok", "id": aid})
    except autonomous_actions.DuplicateActionName as e:
        # Names are unique per user: the clash is always the caller's own action.
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.put("/api/{action_id}", dependencies=_MAY_RUN_ACTIONS)
async def update_action(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    if await owned_action(request, action_id) is None:
        return not_found("Action not found")
    data = await request.json()
    if refused := not_offered(request, data.get("model_id")):
        return refused
    from spark.database import autonomous_actions

    allowed = {
        "name",
        "description",
        "action_prompt",
        "model_id",
        "schedule_type",
        "schedule_config",
        "context_mode",
        "max_failures",
        "max_tokens",
        "is_enabled",
    }
    updates = {k: v for k, v in data.items() if k in allowed}

    try:
        await autonomous_actions.update_action(db, action_id, current_user_guid(request), **updates)
        return JSONResponse({"status": "ok"})
    except autonomous_actions.DuplicateActionName as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.delete("/api/{action_id}")
async def delete_action(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    from spark.database import autonomous_actions

    if await owned_action(request, action_id) is None:
        return not_found("Action not found")
    await autonomous_actions.delete_action(db, action_id, current_user_guid(request))
    return JSONResponse({"status": "ok"})


@router.post("/api/{action_id}/toggle", dependencies=_MAY_RUN_ACTIONS)
async def toggle_action(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    if await owned_action(request, action_id) is None:
        return not_found("Action not found")
    data = await request.json()
    from spark.database import autonomous_actions

    await autonomous_actions.update_action(
        db, action_id, current_user_guid(request), is_enabled=int(bool(data.get("enabled", True)))
    )
    return JSONResponse({"status": "ok"})


@router.get("/api/{action_id}/tools")
async def get_action_tools(request: Request, action_id: int) -> JSONResponse:
    """API: get tool permissions for an action."""
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"tools": []})
    if await owned_action(request, action_id) is None:
        return not_found("Action not found")

    # Get all available tools
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    config = conv_mgr._embedded_tools_config if conv_mgr else {}

    from spark.identity.access import tools_config_for
    from spark.tools.registry import get_builtin_tools

    all_tools = get_builtin_tools(tools_config_for(request, config))

    # Add MCP tools
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr and mcp_mgr._tools_cache:
        for t in mcp_mgr._tools_cache:
            all_tools.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "source": t.get("server", "mcp"),
                }
            )

    # Get action's tool permissions
    from spark.database import autonomous_actions

    permissions = await autonomous_actions.get_tool_permissions(
        db, current_user_guid(request), action_id
    )

    tools = []
    for t in all_tools:
        name = t.get("name", "")
        # Default to allowed if no permission record exists
        perm = permissions.get(name, "allowed")
        tools.append(
            {
                "name": name,
                "description": t.get("description", "")[:80],
                "source": t.get("source", "embedded"),
                "enabled": perm == "allowed",
            }
        )

    return JSONResponse({"tools": tools})


@router.post("/api/{action_id}/tools")
async def update_action_tool(request: Request, action_id: int) -> JSONResponse:
    """API: toggle a tool for an action."""
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    tool_name = data.get("name", "")
    enabled = data.get("enabled", True)
    permission_state = "allowed" if enabled else "denied"

    from spark.database import autonomous_actions

    if not await autonomous_actions.set_tool_permission(
        db, action_id, tool_name, permission_state, current_user_guid(request)
    ):
        return not_found("Action not found")

    return JSONResponse({"status": "ok"})


@router.get("/api/{action_id}/runs")
async def get_runs(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse([])

    from spark.database import autonomous_actions

    if await owned_action(request, action_id) is None:
        return not_found("Action not found")
    runs = await autonomous_actions.get_action_runs(
        db, current_user_guid(request), action_id, limit=20
    )
    return JSONResponse(runs)


@router.post("/api/{action_id}/run-now", dependencies=_MAY_RUN_ACTIONS)
async def run_action_now(request: Request, action_id: int) -> JSONResponse:
    """Trigger an immediate action execution.

    Runs the action using ActionExecutor in a background thread with its
    own MCP connections, identical to how the daemon would execute it.
    """
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    ctx = getattr(request.app.state, "ctx", None)
    if not db or not ctx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    from spark.database import autonomous_actions

    action = await autonomous_actions.get_action(db, action_id, current_user_guid(request))
    if not action:
        return JSONResponse({"error": "Action not found"}, status_code=404)

    import threading

    sync_db = sync_database(request)
    from spark.identity.access import request_permissions

    # Run now acts for the signed-in user, with their permissions.
    permissions = request_permissions(request)
    user_guid = current_user_guid(request)

    def _run() -> None:
        from spark.scheduler.executor import ActionExecutor

        executor = ActionExecutor(
            ctx, "web-run-now", sync_db, permissions=permissions, user_guid=user_guid
        )
        executor.execute(action_id)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return JSONResponse({"status": "running", "message": f"Action '{action['name']}' started."})


@router.post("/api/ai-create", dependencies=_MAY_RUN_ACTIONS)
async def ai_create_message(request: Request) -> JSONResponse:
    """API: send a message to the AI action creation assistant."""
    if r := _guard(request):
        return r
    llm_manager = getattr(request.app.state, "llm_manager", None)
    db = getattr(request.app.state, "database", None)
    if not llm_manager or not llm_manager.active_service or not db:
        return JSONResponse({"error": "LLM not available"}, status_code=503)

    data = await request.json()
    message = data.get("message", "").strip()
    history = data.get("history", [])
    action_name = data.get("action_name", "")
    model_id = data.get("model_id", "")
    conversation_id = data.get("conversation_id")

    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    if refused := not_offered(request, model_id):
        return refused

    from spark.scheduler.creation_tools import (
        ACTION_CREATION_SYSTEM_PROMPT,
        CREATION_TOOLS,
        execute_creation_tool,
    )

    messages = list(history)
    messages.append({"role": "user", "content": message})

    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    # This session's model answers through a service of its own, so one user's
    # choice never changes the shared manager's model for anyone else.
    service = llm_manager
    if model_id and conv_mgr is not None:
        service = (
            await asyncio.to_thread(conv_mgr._get_llm_service_for_model, model_id) or llm_manager
        )
    from spark.identity.access import tools_config_for

    # A copy: the shared configuration never carries one user's guid or permissions.
    config = tools_config_for(request, conv_mgr._embedded_tools_config if conv_mgr else {})
    config["_user_guid"] = current_user_guid(request)
    # The action the assistant creates may use only an offered model.
    config["_offered_models"] = sorted(offered_models(request))

    # Augment system prompt with pre-selected name and model
    system_prompt = ACTION_CREATION_SYSTEM_PROMPT
    if action_name or model_id:
        system_prompt += f"\n\nThe user has already chosen:\n- Action name: {action_name}\n- Model: {model_id}\n\nUse these values when creating the action. Do not ask the user for the name or model again."

    # Inject conversation context if creating from a conversation
    if conversation_id and conv_mgr and not history:
        try:
            conv_messages = await asyncio.to_thread(
                conv_mgr.get_messages, int(conversation_id), current_user_guid(request)
            )
            if conv_messages:
                # Build a summary of the conversation for the AI to analyse
                conv_lines = []
                for msg in conv_messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if (
                        role in ("user", "assistant")
                        and isinstance(content, str)
                        and content.strip()
                    ):
                        # Skip tool result markers and JSON blocks
                        if content.startswith("[TOOL_RESULTS]") or content.startswith("["):
                            continue
                        # Truncate very long messages
                        if len(content) > 500:
                            content = content[:500] + "..."
                        conv_lines.append(f"**{role.title()}:** {content}")

                if conv_lines:
                    conv_summary = "\n".join(conv_lines[-30:])  # Last 30 messages
                    system_prompt += (
                        f"\n\n## Source Conversation\n"
                        f"The user is creating this action from an existing conversation. "
                        f"Analyse the conversation below to understand what the user has been "
                        f"working on, and use it to suggest an appropriate action prompt, "
                        f"schedule, and configuration.\n\n{conv_summary}"
                    )
        except Exception as e:
            logger.warning("Failed to load conversation context: %s", e)

    all_tool_calls: list[dict] = []
    for _ in range(10):
        response = service.invoke_model(
            messages,
            max_tokens=4096,
            temperature=0.7,
            tools=CREATION_TOOLS,
            system=system_prompt,
        )

        stop_reason = response.get("stop_reason", "end_turn")

        if stop_reason == "tool_use" and response.get("tool_use"):
            tool_results = []
            for tc in response["tool_use"]:
                result_text = await asyncio.to_thread(
                    execute_creation_tool,
                    tc["name"],
                    tc.get("input", {}),
                    config,
                    sync_database(request),
                )
                all_tool_calls.append(
                    {
                        "name": tc["name"],
                        "input": tc.get("input", {}),
                        "result": result_text,
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": result_text,
                    }
                )

            messages.append({"role": "assistant", "content": response.get("content_blocks", [])})
            messages.append({"role": "user", "content": tool_results})
            continue

        return JSONResponse(
            {
                "response": response.get("content", ""),
                "tool_calls": all_tool_calls,
                "history": messages,
            }
        )

    return JSONResponse(
        {
            "response": "Maximum tool call iterations reached. Please try again.",
            "tool_calls": all_tool_calls,
            "history": messages,
        }
    )
