"""Route registration for the Spark web interface."""

from __future__ import annotations

from fastapi import FastAPI

from spark.web.endpoints.account import router as account_router
from spark.web.endpoints.actions import router as actions_router
from spark.web.endpoints.administration import router as administration_router
from spark.web.endpoints.audit import router as audit_router
from spark.web.endpoints.auth import router as auth_router
from spark.web.endpoints.bundles import router as bundles_router
from spark.web.endpoints.chat import router as chat_router
from spark.web.endpoints.conversations import router as conversations_router
from spark.web.endpoints.debate import router as debate_router
from spark.web.endpoints.files import router as files_router
from spark.web.endpoints.help import router as help_router
from spark.web.endpoints.identity import router as identity_router
from spark.web.endpoints.knowledge import router as knowledge_router
from spark.web.endpoints.lifecycle import router as lifecycle_router
from spark.web.endpoints.main_menu import router as main_menu_router
from spark.web.endpoints.mcp_servers import router as mcp_servers_router
from spark.web.endpoints.memories import router as memories_router
from spark.web.endpoints.panel import router as panel_router
from spark.web.endpoints.provider_guides import router as provider_guides_router
from spark.web.endpoints.settings import router as settings_router
from spark.web.endpoints.skills import router as skills_router
from spark.web.endpoints.streaming import router as streaming_router
from spark.web.endpoints.usage import router as usage_router
from spark.web.endpoints.voice import router as voice_router


def register_routes(app: FastAPI) -> None:
    """Register all endpoint routers."""
    app.include_router(auth_router)
    app.include_router(identity_router)
    app.include_router(lifecycle_router)
    app.include_router(account_router)
    app.include_router(administration_router)
    app.include_router(audit_router)
    app.include_router(main_menu_router)
    app.include_router(settings_router)
    app.include_router(conversations_router)
    app.include_router(chat_router)
    app.include_router(streaming_router)
    app.include_router(debate_router)
    app.include_router(panel_router)
    app.include_router(voice_router)
    app.include_router(memories_router)
    app.include_router(files_router)
    app.include_router(skills_router)
    app.include_router(knowledge_router)
    app.include_router(actions_router)
    app.include_router(mcp_servers_router)
    app.include_router(help_router)
    app.include_router(provider_guides_router)
    app.include_router(usage_router)
    app.include_router(bundles_router)
