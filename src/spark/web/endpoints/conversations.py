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
        return JSONResponse({"error": "Conversation manager not initialised"}, status_code=503)

    name = data.get("name", "New Conversation")
    model_id = data.get("model_id", "")
    instructions = data.get("instructions")
    web_search = data.get("web_search_enabled", False)
    user_guid = _get_user_guid(request)

    try:
        cid = conv_mgr.create_conversation(
            name, model_id, user_guid,
            instructions=instructions,
            web_search_enabled=web_search,
        )
        return JSONResponse({"id": cid, "name": name})
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

    conversations.update_conversation(conv_mgr._db, conversation_id, user_guid, name=name)
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

    conversations.update_conversation(conv_mgr._db, conversation_id, user_guid, model_id=model_id)
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

    conversations.update_conversation(conv_mgr._db, conversation_id, user_guid, is_favourite=is_favourite)
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
            filtered = [{"id": default_model_id, "name": default_model_id, "provider": "configured"}]
        return JSONResponse({"models": filtered, "default_model": default_model_id, "mandatory": True})

    return JSONResponse({"models": all_models, "default_model": default_model_id or None, "mandatory": False})


def _get_user_guid(request: Request) -> str:
    """Get the persistent user GUID from app state."""
    return getattr(request.app.state, "user_guid", "default")
