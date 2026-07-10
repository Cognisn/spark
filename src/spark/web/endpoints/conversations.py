"""Conversation management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/conversations")


@router.get("", response_class=HTMLResponse)
async def conversations_page(request: Request) -> HTMLResponse:
    """Render the conversations list page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "conversations.html")


@router.get("/api/list")
async def list_conversations(request: Request) -> JSONResponse:
    """API: list all active conversations."""
    ctx = request.app.state.ctx
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])

    user_guid = _get_user_guid(request)
    convs = conv_mgr.get_conversations(user_guid)
    return JSONResponse(convs)


@router.post("/api/create")
async def create_conversation(request: Request) -> JSONResponse:
    """API: create a new conversation."""
    data = await request.json()
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse(
            {"error": "Conversation manager not initialised"}, status_code=503
        )

    name = data.get("name", "New Conversation")
    model_id = data.get("model_id", "")
    instructions = data.get("instructions")
    web_search = data.get("web_search_enabled", False)
    conversation_type = data.get("conversation_type", "standard")
    user_guid = _get_user_guid(request)

    # Validate a debate payload BEFORE creating anything, so a bad request
    # never leaves an orphan conversation row.
    debate = None
    if conversation_type == "debate":
        debate = data.get("debate") or {}
        agents = debate.get("agents") or {}
        if not all(
            role in agents and (agents[role] or {}).get("model_id")
            for role in ("pro", "con", "judge")
        ):
            return JSONResponse(
                {"error": "Debate requires pro, con and judge agents with models"},
                status_code=400,
            )
        if not (debate.get("topic") or "").strip():
            return JSONResponse({"error": "Debate requires a topic"}, status_code=400)
        for role, spec in agents.items():
            for key in ("allowed_tools", "allowed_skills"):
                value = (spec or {}).get(key)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    return JSONResponse(
                        {"error": f"{key} for {role} must be a list of tool/skill names"},
                        status_code=400,
                    )

    # Validate a panel payload BEFORE creating anything, for the same reason.
    panel = None
    panel_agents = None
    if conversation_type == "panel":
        panel = data.get("panel") or {}
        if not (panel.get("topic") or "").strip():
            return JSONResponse({"error": "Panel requires a topic"}, status_code=400)
        moderator = panel.get("moderator") or {}
        if not moderator.get("model_id"):
            return JSONResponse(
                {"error": "Panel requires a moderator with a model"}, status_code=400
            )
        panellists = panel.get("panellists") or []
        if not isinstance(panellists, list) or not 2 <= len(panellists) <= 5:
            return JSONResponse(
                {"error": "Panel requires 2 to 5 panellists"}, status_code=400
            )
        human = panel.get("human") or None
        names = [(p or {}).get("name", "").strip() for p in panellists]
        if human is not None:
            human_name = (human.get("name") or "").strip()
            if not human_name:
                return JSONResponse(
                    {"error": "Human panellist requires a name"}, status_code=400
                )
            names.append(human_name)
        if any(not n for n in names) or len({n.casefold() for n in names}) != len(names):
            return JSONResponse(
                {"error": "Panellists need distinct, non-empty names"}, status_code=400
            )
        if any(not (p or {}).get("model_id") for p in panellists):
            return JSONResponse(
                {"error": "Every AI panellist requires a model"}, status_code=400
            )
        specs = [("moderator", moderator)] + [
            (f"panellist:{i}", p) for i, p in enumerate(panellists, start=1)
        ]
        for role, spec in specs:
            for key in ("allowed_tools", "allowed_skills"):
                value = (spec or {}).get(key)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                ):
                    return JSONResponse(
                        {"error": f"{key} for {role} must be a list of tool/skill names"},
                        status_code=400,
                    )
        panel_agents = {
            "moderator": {
                "model_id": moderator["model_id"],
                "brief": moderator.get("brief"),
                "allowed_skills": moderator.get("allowed_skills"),
                "display_name": "Moderator",
            }
        }
        for i, spec in enumerate(panellists, start=1):
            panel_agents[f"panellist:{i}"] = {
                "model_id": spec["model_id"],
                "brief": spec.get("brief"),
                "allowed_tools": spec.get("allowed_tools"),
                "allowed_skills": spec.get("allowed_skills"),
                "display_name": spec["name"].strip(),
            }
        if human is not None:
            panel_agents[f"panellist:{len(panellists) + 1}"] = {
                "model_id": "",
                "brief": None,
                "display_name": (human.get("name") or "").strip(),
                "is_human": True,
            }

    try:
        cid = conv_mgr.create_conversation(
            name,
            model_id,
            user_guid,
            instructions=instructions,
            web_search_enabled=web_search,
        )
        if conversation_type == "debate" and debate is not None:
            from spark.database import debates

            db = conv_mgr._db
            ph = db.placeholder
            db.execute(
                f"UPDATE conversations SET conversation_type = 'debate' WHERE id = {ph}",
                (cid,),
            )
            db.commit()
            debates.create_debate(
                db,
                cid,
                debate["topic"].strip(),
                debate.get("rounds_mode", "fixed"),
                debate.get("max_rounds"),
                user_guid,
                debate["agents"],
            )
        if conversation_type == "panel" and panel_agents is not None:
            from spark.database import debates

            db = conv_mgr._db
            ph = db.placeholder
            db.execute(
                f"UPDATE conversations SET conversation_type = 'panel' WHERE id = {ph}",
                (cid,),
            )
            db.commit()
            debates.create_debate(
                db,
                cid,
                panel["topic"].strip(),
                panel.get("rounds_mode", "fixed"),
                panel.get("max_rounds"),
                user_guid,
                panel_agents,
            )
        if data.get("kg_local_enabled"):
            db = conv_mgr._db
            ph = db.placeholder
            db.execute(
                f"UPDATE conversations SET kg_local_enabled = 1 WHERE id = {ph}", (cid,)
            )
            db.commit()
        return JSONResponse({"id": cid, "name": name, "conversation_type": conversation_type})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.delete("/api/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """API: delete a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    user_guid = _get_user_guid(request)
    conv_mgr.delete_conversation(conversation_id, user_guid)
    return JSONResponse({"status": "ok"})


@router.get("/api/search")
async def search_conversations(request: Request) -> JSONResponse:
    """API: search conversations by keyword."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse([])

    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse([])

    user_guid = _get_user_guid(request)
    from spark.database import conversations

    results = conversations.search_conversations(conv_mgr._db, query, user_guid)
    return JSONResponse(results)


@router.post("/api/{conversation_id}/rename")
async def rename_conversation(request: Request, conversation_id: int) -> JSONResponse:
    """API: rename a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name required"}, status_code=400)

    user_guid = _get_user_guid(request)
    from spark.database import conversations

    conversations.update_conversation(
        conv_mgr._db, conversation_id, user_guid, name=name
    )
    return JSONResponse({"status": "ok"})


@router.post("/api/{conversation_id}/model")
async def change_model(request: Request, conversation_id: int) -> JSONResponse:
    """API: change the model for a conversation."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    model_id = data.get("model_id", "").strip()
    if not model_id:
        return JSONResponse({"error": "model_id required"}, status_code=400)

    user_guid = _get_user_guid(request)
    from spark.database import conversations

    conversations.update_conversation(
        conv_mgr._db, conversation_id, user_guid, model_id=model_id
    )
    return JSONResponse({"status": "ok"})


@router.post("/api/{conversation_id}/favourite")
async def toggle_favourite(request: Request, conversation_id: int) -> JSONResponse:
    """API: toggle favourite status."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return JSONResponse({"error": "Not initialised"}, status_code=503)

    data = await request.json()
    is_favourite = int(bool(data.get("favourite", False)))
    user_guid = _get_user_guid(request)

    from spark.database import conversations

    conversations.update_conversation(
        conv_mgr._db, conversation_id, user_guid, is_favourite=is_favourite
    )
    return JSONResponse({"status": "ok"})


@router.get("/api/models")
async def list_models(request: Request) -> JSONResponse:
    """API: list available models, respecting default_model settings."""
    llm_manager = getattr(request.app.state, "llm_manager", None)
    if not llm_manager:
        return JSONResponse({"models": [], "default_model": None, "mandatory": False})

    all_models = llm_manager.list_all_models()
    ctx = request.app.state.ctx

    default_model_id = ctx.settings.get("default_model.model_id", "")
    mode = ctx.settings.get("default_model.mode", "default")
    mandatory = mode == "mandatory" and bool(default_model_id)

    if mandatory:
        # Only return the mandatory model
        filtered = [m for m in all_models if m["id"] == default_model_id]
        if not filtered:
            # Model not found in available models — still return it so user sees something
            filtered = [
                {
                    "id": default_model_id,
                    "name": default_model_id,
                    "provider": "configured",
                }
            ]
        return JSONResponse(
            {"models": filtered, "default_model": default_model_id, "mandatory": True}
        )

    return JSONResponse(
        {
            "models": all_models,
            "default_model": default_model_id or None,
            "mandatory": False,
        }
    )


def _get_user_guid(request: Request) -> str:
    """Get the persistent user GUID from app state."""
    return getattr(request.app.state, "user_guid", "default")
