"""Autonomous action management endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions")


def _user(request: Request) -> str:
    return getattr(request.app.state, "user_guid", "default")


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

    actions = autonomous_actions.get_enabled_actions(db.connection, _user(request))
    # Also get disabled ones
    all_actions = _get_all_actions(db.connection)
    return JSONResponse(all_actions)


@router.post("/api/create")
async def create_action(request: Request) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    from spark.database import autonomous_actions

    try:
        aid = autonomous_actions.create_action(
            db.connection,
            name=data["name"],
            action_prompt=data["prompt"],
            model_id=data["model_id"],
            user_guid=_user(request),
            description=data.get("description"),
            schedule_type=data.get("schedule_type", "one_off"),
            schedule_config=data.get("schedule_config"),
            context_mode=data.get("context_mode", "fresh"),
            max_failures=int(data.get("max_failures", 3)),
            max_tokens=int(data.get("max_tokens", 8192)),
        )
        return JSONResponse({"status": "ok", "id": aid})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.put("/api/{action_id}")
async def update_action(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
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
        autonomous_actions.update_action(db.connection, action_id, _user(request), **updates)
        return JSONResponse({"status": "ok"})
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

    autonomous_actions.delete_action(db.connection, action_id, _user(request))
    return JSONResponse({"status": "ok"})


@router.post("/api/{action_id}/toggle")
async def toggle_action(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    from spark.database import autonomous_actions

    autonomous_actions.update_action(
        db.connection, action_id, _user(request), is_enabled=int(bool(data.get("enabled", True)))
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

    # Get all available tools
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    config = conv_mgr._embedded_tools_config if conv_mgr else {}

    from spark.tools.registry import get_builtin_tools

    all_tools = get_builtin_tools(config)

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
    cursor = db.connection.execute(
        f"SELECT tool_name, permission_state FROM action_tool_permissions WHERE action_id = {db.connection.placeholder}",
        (action_id,),
    )
    permissions = {row[0]: row[1] for row in cursor.fetchall()}

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

    ph = db.connection.placeholder
    upsert = db.connection.backend.upsert_sql(
        "action_tool_permissions",
        ["action_id", "tool_name", "permission_state", "user_guid"],
        ["action_id", "tool_name"],
    )
    db.connection.execute(upsert, (action_id, tool_name, permission_state, _user(request)))
    db.connection.commit()

    return JSONResponse({"status": "ok"})


@router.get("/api/{action_id}/runs")
async def get_runs(request: Request, action_id: int) -> JSONResponse:
    if r := _guard(request):
        return r
    db = getattr(request.app.state, "database", None)
    if not db:
        return JSONResponse([])

    from spark.database import autonomous_actions

    runs = autonomous_actions.get_action_runs(db.connection, action_id, limit=20)
    return JSONResponse(runs)


@router.post("/api/{action_id}/run-now")
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

    action = autonomous_actions.get_action(db.connection, action_id, _user(request))
    if not action:
        return JSONResponse({"error": "Action not found"}, status_code=404)

    import threading

    def _run() -> None:
        from spark.scheduler.executor import ActionExecutor

        executor = ActionExecutor(ctx, "web-run-now")
        executor.execute(action_id)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return JSONResponse({"status": "running", "message": f"Action '{action['name']}' started."})


@router.post("/api/ai-create")
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

    from spark.scheduler.creation_tools import (
        ACTION_CREATION_SYSTEM_PROMPT,
        CREATION_TOOLS,
        execute_creation_tool,
    )

    # Set the model for this creation session
    if model_id:
        try:
            llm_manager.set_model(model_id)
        except ValueError:
            pass

    messages = list(history)
    messages.append({"role": "user", "content": message})

    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    config = conv_mgr._embedded_tools_config if conv_mgr else {}
    config["_user_guid"] = _user(request)

    # Augment system prompt with pre-selected name and model
    system_prompt = ACTION_CREATION_SYSTEM_PROMPT
    if action_name or model_id:
        system_prompt += f"\n\nThe user has already chosen:\n- Action name: {action_name}\n- Model: {model_id}\n\nUse these values when creating the action. Do not ask the user for the name or model again."

    # Inject conversation context if creating from a conversation
    if conversation_id and conv_mgr and not history:
        try:
            conv_messages = conv_mgr.get_messages(int(conversation_id))
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
        response = llm_manager.invoke_model(
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
                result_text = execute_creation_tool(
                    tc["name"],
                    tc.get("input", {}),
                    config,
                    db.connection,
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


def _get_all_actions(db_conn: Any) -> list[dict]:
    """Get all actions (enabled and disabled)."""
    cursor = db_conn.execute("SELECT * FROM autonomous_actions ORDER BY name")
    return [dict(row) for row in cursor.fetchall()]
