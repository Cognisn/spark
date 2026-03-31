"""Route registration for the Spark web interface."""

from __future__ import annotations

from fastapi import FastAPI

from spark.web.endpoints.actions import router as actions_router
from spark.web.endpoints.auth import router as auth_router
from spark.web.endpoints.help import router as help_router
from spark.web.endpoints.mcp_servers import router as mcp_servers_router
from spark.web.endpoints.chat import router as chat_router
from spark.web.endpoints.conversations import router as conversations_router
from spark.web.endpoints.main_menu import router as main_menu_router
from spark.web.endpoints.memories import router as memories_router
from spark.web.endpoints.settings import router as settings_router
from spark.web.endpoints.streaming import router as streaming_router


def register_routes(app: FastAPI) -> None:
    """Register all endpoint routers."""
    app.include_router(auth_router)
    app.include_router(main_menu_router)
    app.include_router(settings_router)
    app.include_router(conversations_router)
    app.include_router(chat_router)
    app.include_router(streaming_router)
    app.include_router(memories_router)
    app.include_router(actions_router)
    app.include_router(mcp_servers_router)
    app.include_router(help_router)
