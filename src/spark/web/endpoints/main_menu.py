"""Main menu and dashboard endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from fastapi.responses import JSONResponse

router = APIRouter()


@router.post("/api/heartbeat")
async def heartbeat(request: Request) -> JSONResponse:
    """Browser heartbeat — resets the inactivity counter."""
    request.app.state._last_heartbeat = __import__("time").time()
    return JSONResponse({"status": "ok"})


@router.post("/api/open-folder")
async def open_folder(request: Request) -> JSONResponse:
    """Open a system folder in the file browser."""
    import subprocess
    import sys

    folder_type = request.query_params.get("type", "logs")

    from konfig.paths import log_dir
    from spark.core.application import _get_data_path

    if folder_type == "logs":
        path = log_dir("spark")
    elif folder_type == "data":
        path = _get_data_path()
    else:
        return JSONResponse({"error": "Unknown folder type"}, status_code=400)

    path.mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])

    return JSONResponse({"status": "ok", "path": str(path)})


@router.post("/api/quit")
async def quit_app(request: Request) -> JSONResponse:
    """Shut down the Spark application."""
    import asyncio
    import os
    import signal
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Quit requested via web UI — shutting down")

    # Schedule shutdown after response is sent
    async def _shutdown() -> None:
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.ensure_future(_shutdown())
    return JSONResponse({"status": "ok"})


@router.get("/loading", response_class=HTMLResponse)
async def loading_page(request: Request) -> HTMLResponse:
    """Loading page shown during background initialisation."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "loading.html")


@router.get("/loading/api/status")
async def loading_status(request: Request) -> JSONResponse:
    """API: poll initialisation status."""
    status = getattr(request.app.state, "init_status", {"ready": True, "stage": "Ready"})
    return JSONResponse(status)


@router.get("/", response_model=None)
async def index(request: Request) -> Response:
    """Root page — redirect to welcome if first run, otherwise show menu."""
    if request.app.state.first_run:
        return RedirectResponse(url="/welcome", status_code=303)
    return _render_dashboard(request)


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request) -> HTMLResponse:
    """First-run welcome page directing user to settings."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "welcome.html")


@router.get("/menu", response_class=HTMLResponse)
async def main_menu(request: Request) -> HTMLResponse:
    """Main menu / dashboard."""
    return _render_dashboard(request)


def _render_dashboard(request: Request) -> Response:
    templates = request.app.state.templates
    ctx = request.app.state.ctx
    llm_manager = getattr(request.app.state, "llm_manager", None)

    mcp_manager = getattr(request.app.state, "mcp_manager", None)

    providers = _get_provider_summary(ctx, llm_manager)
    embedded_tools = _get_embedded_tool_summary(ctx)
    mcp_stats = _get_mcp_stats(ctx, embedded_tools, mcp_manager)
    mcp_servers = _get_mcp_server_list(ctx, embedded_tools, mcp_manager)
    recent_conversations = _get_recent_conversations(request)
    favourite_conversations = _get_favourite_conversations(request)

    return templates.TemplateResponse(request, "main_menu.html", {
        "providers": providers,
        "tools": embedded_tools,
        "mcp_stats": mcp_stats,
        "mcp_servers": mcp_servers,
        "recent_conversations": recent_conversations,
        "favourite_conversations": favourite_conversations,
    })


def _get_provider_summary(ctx: object, llm_manager: Any | None) -> list[dict]:
    """Build a summary of configured LLM providers with model counts."""
    settings = ctx.settings  # type: ignore[union-attr]
    provider_map = {
        "anthropic": {"name": "Anthropic", "icon": "bi-robot"},
        "aws_bedrock": {"name": "AWS Bedrock", "icon": "bi-cloud"},
        "ollama": {"name": "Ollama", "icon": "bi-cpu"},
        "google_gemini": {"name": "Google Gemini", "icon": "bi-stars"},
        "xai": {"name": "X.AI", "icon": "bi-lightning"},
    }

    # Build model counts per provider from the llm_manager
    model_counts: dict[str, int] = {}
    if llm_manager:
        for model in llm_manager.list_all_models():
            provider_name = model.get("provider", "")
            model_counts[provider_name] = model_counts.get(provider_name, 0) + 1

    # Map provider keys to display names for count lookup
    key_to_display = {
        "anthropic": "Anthropic",
        "aws_bedrock": "AWS Bedrock",
        "ollama": "Ollama",
        "google_gemini": "Google Gemini",
        "xai": "X.AI",
    }

    providers = []
    for key, meta in provider_map.items():
        enabled = bool(settings.get(f"providers.{key}.enabled", False))
        display_name = key_to_display[key]
        count = model_counts.get(display_name, 0)
        providers.append({
            "key": key,
            "enabled": enabled,
            "model_count": count,
            **meta,
        })
    return providers


def _get_embedded_tool_summary(ctx: object) -> list[dict]:
    """Build a summary of embedded tool categories with tool counts."""
    settings = ctx.settings  # type: ignore[union-attr]
    categories = [
        {"key": "filesystem", "name": "Filesystem", "icon": "bi-folder2-open", "has_mode": True},
        {"key": "documents", "name": "Documents", "icon": "bi-file-earmark-richtext", "has_mode": True},
        {"key": "archives", "name": "Archives", "icon": "bi-file-zip", "has_mode": True},
        {"key": "web", "name": "Web", "icon": "bi-globe2", "has_mode": False},
    ]

    # Get actual tool counts from the registry
    from spark.tools.registry import get_builtin_tools

    config = {"embedded_tools": {}}
    for cat in categories:
        cat_config: dict[str, Any] = {
            "enabled": bool(settings.get(f"embedded_tools.{cat['key']}.enabled", True)),
            "mode": settings.get(f"embedded_tools.{cat['key']}.mode", "read") if cat["has_mode"] else None,
        }
        if cat["key"] == "filesystem":
            cat_config["allowed_paths"] = settings.get("embedded_tools.filesystem.allowed_paths", []) or []
        config["embedded_tools"][cat["key"]] = cat_config
    all_tools = get_builtin_tools(config)

    # Map tool names to categories for counting
    from spark.tools.filesystem import get_tools as fs_tools
    from spark.tools.documents import get_tools as doc_tools
    from spark.tools.archives import get_tools as arc_tools
    from spark.tools.web import get_tools as web_tools

    cat_tool_names = {
        "filesystem": {t["name"] for t in fs_tools(mode="read_write")},
        "documents": {t["name"] for t in doc_tools()},
        "archives": {t["name"] for t in arc_tools(mode="extract")},
        "web": {t["name"] for t in web_tools()},
    }

    tools = []
    for cat in categories:
        enabled = bool(settings.get(f"embedded_tools.{cat['key']}.enabled", True))
        mode = settings.get(f"embedded_tools.{cat['key']}.mode", None) if cat["has_mode"] else None
        # Count active tools for this category
        active_count = sum(1 for t in all_tools if t["name"] in cat_tool_names.get(cat["key"], set()))
        total_count = len(cat_tool_names.get(cat["key"], set()))
        tools.append({
            "key": cat["key"],
            "name": cat["name"],
            "icon": cat["icon"],
            "enabled": enabled,
            "mode": mode,
            "tool_count": active_count if enabled else 0,
            "total_tool_count": total_count,
        })
    return tools


def _get_mcp_stats(ctx: object, embedded_tools: list[dict], mcp_manager: Any = None) -> dict:
    """Build MCP summary stats (servers, failures, total tools)."""
    embedded_tool_count = sum(t["tool_count"] for t in embedded_tools)
    embedded_enabled = any(t["enabled"] for t in embedded_tools)

    mcp_configured = 1 if embedded_enabled else 0
    mcp_failed = 0
    total_tools = embedded_tool_count

    if mcp_manager:
        for name, client in mcp_manager.servers.items():
            mcp_configured += 1
            if not client.connected:
                mcp_failed += 1
        # Count tools from cache
        if mcp_manager._tools_cache:
            total_tools += len(mcp_manager._tools_cache)

    return {
        "servers_configured": mcp_configured,
        "servers_failed": mcp_failed,
        "total_tools": total_tools,
    }


def _get_mcp_server_list(ctx: object, embedded_tools: list[dict], mcp_manager: Any = None) -> list[dict]:
    """Build the list of MCP server cards (including embedded)."""
    servers = []

    # Embedded tools as a virtual server
    embedded_tool_count = sum(t["tool_count"] for t in embedded_tools)
    if any(t["enabled"] for t in embedded_tools):
        servers.append({
            "name": "Embedded Tools",
            "icon": "bi-puzzle",
            "type": "embedded",
            "connected": True,
            "tool_count": embedded_tool_count,
            "settings_url": "/settings#section-embedded-tools",
        })

    # Real MCP servers
    if mcp_manager:
        tools_cache = mcp_manager._tools_cache or []
        for name, client in mcp_manager.servers.items():
            tool_count = sum(1 for t in tools_cache if t.get("server") == name)
            servers.append({
                "name": name,
                "icon": "bi-hdd-network",
                "type": "mcp",
                "connected": client.connected,
                "tool_count": tool_count,
                "settings_url": "/settings#section-mcp-servers",
            })

    return servers


def _get_recent_conversations(request: Any) -> list[dict]:
    """Get the 5 most recently active non-favourite conversations."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return []
    try:
        all_convs = conv_mgr.get_conversations("default")
        non_favs = [c for c in all_convs if not c.get("is_favourite")]
        return non_favs[:5]
    except Exception:
        return []


def _get_favourite_conversations(request: Any) -> list[dict]:
    """Get all favourite conversations."""
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if not conv_mgr:
        return []
    try:
        all_convs = conv_mgr.get_conversations("default")
        return [c for c in all_convs if c.get("is_favourite")]
    except Exception:
        return []
