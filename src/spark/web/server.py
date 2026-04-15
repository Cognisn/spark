"""FastAPI web server for Spark."""

from __future__ import annotations

import logging
import os
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import spark
from spark.web.auth import AuthManager
from spark.web.session import SessionManager

if TYPE_CHECKING:
    from konfig import AppContext

    from spark.llm.manager import LLMManager

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def create_app(ctx: AppContext, *, first_run: bool = False) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Spark", version=spark.__version__, docs_url=None, redoc_url=None)

    # -- Shared state ---------------------------------------------------------
    timeout = ctx.settings.get("interface.session_timeout_minutes", 60, cast=int)
    auth_manager = AuthManager()
    session_manager = SessionManager(timeout_minutes=timeout)

    from spark.core.user_guid import get_user_guid

    app.state.ctx = ctx
    app.state.auth = auth_manager
    app.state.session = session_manager
    app.state.first_run = first_run
    app.state.init_status = {"ready": True, "error": False, "stage": "Ready"}
    app.state.user_guid = get_user_guid(ctx)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.globals["version"] = spark.__version__
    templates.env.globals["actions_enabled"] = lambda: (
        bool(app.state.ctx.settings.get("autonomous_actions.enabled", False))
        if hasattr(app.state, "ctx") and app.state.ctx
        else False
    )
    app.state.templates = templates

    # -- Static files ---------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # -- Routes ---------------------------------------------------------------
    from spark.web.endpoints import register_routes

    register_routes(app)

    # -- Auth middleware -------------------------------------------------------
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        public = {"/login", "/auto-login", "/loading", "/static", "/api/auth", "/favicon.ico"}
        path = request.url.path
        if any(path.startswith(p) for p in public):
            return await call_next(request)

        session_id = request.cookies.get("spark_session")
        if not session_id or not session_manager.validate(session_id):
            return RedirectResponse(url="/login", status_code=303)

        return await call_next(request)

    return app


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Find a random available TCP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _resolve_secret(ctx: AppContext, value: str | None) -> str:
    """Resolve a secret:// URI via the konfig secrets backend, or return as-is."""
    if not value:
        return ""
    if hasattr(ctx, "secrets") and ctx.secrets:
        resolved = ctx.secrets.resolve_uri(value)
        return str(resolved) if resolved else ""
    return str(value)


def _init_providers(ctx: AppContext) -> "LLMManager":
    """Initialise LLM providers from settings and return an LLMManager."""
    from spark.llm.manager import LLMManager

    mgr = LLMManager()
    settings = ctx.settings

    # Anthropic Direct
    if settings.get("providers.anthropic.enabled"):
        try:
            from spark.llm.anthropic_direct import AnthropicDirectProvider

            api_key = _resolve_secret(ctx, settings.get("providers.anthropic.api_key", ""))
            if api_key:
                provider = AnthropicDirectProvider(api_key=api_key)
                mgr.register_provider(provider)
        except Exception as e:
            logger.warning("Failed to init Anthropic provider: %s", e)

    # AWS Bedrock
    if settings.get("providers.aws_bedrock.enabled"):
        try:
            from spark.llm.bedrock import BedrockProvider

            region = settings.get("providers.aws_bedrock.region", "us-east-1")
            auth_method = settings.get("providers.aws_bedrock.auth_method", "sso")
            profile = settings.get("providers.aws_bedrock.profile")
            access_key = _resolve_secret(ctx, settings.get("providers.aws_bedrock.access_key"))
            secret_key = _resolve_secret(ctx, settings.get("providers.aws_bedrock.secret_key"))
            session_token = _resolve_secret(
                ctx, settings.get("providers.aws_bedrock.session_token")
            )

            # Only pass explicit keys when auth method is not SSO.
            if auth_method in ("iam", "session") and access_key and secret_key:
                provider = BedrockProvider(
                    region=region,
                    access_key=access_key,
                    secret_key=secret_key,
                    session_token=session_token or None,
                )
            else:
                provider = BedrockProvider(region=region, profile=profile)
            mgr.register_provider(provider)
        except Exception as e:
            logger.warning("Failed to init AWS Bedrock provider: %s", e)

    # Ollama
    if settings.get("providers.ollama.enabled"):
        try:
            from spark.llm.ollama import OllamaProvider

            base_url = settings.get("providers.ollama.base_url", "http://localhost:11434")
            provider = OllamaProvider(base_url=base_url)
            mgr.register_provider(provider)
        except Exception as e:
            logger.warning("Failed to init Ollama provider: %s", e)

    # Google Gemini
    if settings.get("providers.google_gemini.enabled"):
        try:
            from spark.llm.google_gemini import GoogleGeminiProvider

            api_key = _resolve_secret(ctx, settings.get("providers.google_gemini.api_key", ""))
            if api_key:
                provider = GoogleGeminiProvider(api_key=api_key)
                mgr.register_provider(provider)
        except Exception as e:
            logger.warning("Failed to init Google Gemini provider: %s", e)

    # X.AI
    if settings.get("providers.xai.enabled"):
        try:
            from spark.llm.xai import XAIProvider

            api_key = _resolve_secret(ctx, settings.get("providers.xai.api_key", ""))
            if api_key:
                provider = XAIProvider(api_key=api_key)
                mgr.register_provider(provider)
        except Exception as e:
            logger.warning("Failed to init X.AI provider: %s", e)

    if mgr.providers:
        logger.info("LLM providers ready: %s", ", ".join(mgr.providers.keys()))
    else:
        logger.warning("No LLM providers configured")

    return mgr


def _start_tray_daemon_if_needed() -> None:
    """Start the system tray daemon as a detached process if not already running."""
    import subprocess
    import sys

    from spark.core.application import _get_data_path

    pid_file = _get_data_path() / "spark_daemon_tray.pid"

    # Check if already running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            logger.info("Tray daemon already running (PID %d)", pid)
            return
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    # Start the daemon
    cmd = [sys.executable, "-m", "spark.daemon.tray"]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    log_file = _get_data_path() / "spark_daemon_tray.log"
    with open(log_file, "a") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, **kwargs)

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))
    logger.info("Tray daemon started (PID %d)", proc.pid)


async def _connect_and_list_mcp(mcp_manager: Any) -> None:
    """Connect all MCP servers and populate tool cache."""
    results = await mcp_manager.connect_all()
    connected = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    logger.info("MCP servers: %d connected, %d failed", connected, failed)
    tools = await mcp_manager.list_all_tools()
    logger.info("MCP tool cache: %d tools loaded", len(tools))


def _background_init(app: FastAPI, ctx: AppContext) -> None:
    """Heavy initialisation running in a background thread after server start."""
    import threading

    def _init() -> None:
        status = app.state.init_status
        try:
            # Step 1: LLM Providers
            status["stage"] = "Connecting to LLM providers..."
            llm_manager = _init_providers(ctx)
            app.state.llm_manager = llm_manager

            # Step 2: Database
            status["stage"] = "Initialising database..."
            from spark.database import Database

            database = Database.from_settings(ctx.settings)
            app.state.database = database

            # Step 3: Conversation Manager
            status["stage"] = "Setting up conversation manager..."
            from spark.core.conversation_manager import ConversationManager
            from spark.llm.context_limits import ContextLimitResolver

            context_limits = ContextLimitResolver(
                ctx.settings.get("context_limits") if ctx.settings.get("context_limits") else None
            )
            conv_settings = ctx.settings.get("conversation") or {}
            embedded_tools_config = {"embedded_tools": ctx.settings.get("embedded_tools") or {}}

            # Resolve secret:// URIs within embedded tools config
            for _cat, cat_config in embedded_tools_config.get("embedded_tools", {}).items():
                if isinstance(cat_config, dict):
                    for key, val in cat_config.items():
                        if isinstance(val, str) and val.startswith("secret://"):
                            cat_config[key] = _resolve_secret(ctx, val)

            # Pass prompt inspection settings into the config
            if ctx.settings.get("prompt_inspection.enabled"):
                embedded_tools_config["_prompt_inspection_enabled"] = True
                embedded_tools_config["_prompt_inspection_level"] = ctx.settings.get(
                    "prompt_inspection.level", "standard"
                )
                embedded_tools_config["_prompt_inspection_action"] = ctx.settings.get(
                    "prompt_inspection.action", "warn"
                )

            # Step 3b: Connect saved MCP servers
            status["stage"] = "Connecting MCP servers..."
            mcp_manager = None
            try:
                import asyncio as _asyncio

                import yaml as _yaml

                from spark.core.application import _get_config_path
                from spark.mcp_integration.manager import MCPManager

                config_path = _get_config_path()
                if config_path.exists():
                    raw_config = _yaml.safe_load(config_path.read_text()) or {}
                    mcp_config = raw_config.get("mcp", {})
                    if mcp_config.get("servers"):
                        mcp_manager = MCPManager.from_config(raw_config)

                        # Create a persistent event loop for MCP connections
                        # This loop stays alive so async generators aren't garbage collected
                        mcp_loop = _asyncio.new_event_loop()

                        def _run_mcp_loop() -> None:
                            _asyncio.set_event_loop(mcp_loop)
                            mcp_loop.run_forever()

                        mcp_thread = threading.Thread(target=_run_mcp_loop, daemon=True)
                        mcp_thread.start()

                        # Connect and list tools in the persistent loop
                        future = _asyncio.run_coroutine_threadsafe(
                            _connect_and_list_mcp(mcp_manager), mcp_loop
                        )
                        future.result(timeout=60)

                        app.state._mcp_loop = mcp_loop  # Keep reference to prevent GC

                app.state.mcp_manager = mcp_manager
            except Exception as e:
                logger.warning("MCP server init failed (non-fatal): %s", e)

            app.state.conversation_manager = ConversationManager(
                database.connection,
                llm_manager,
                context_limits,
                global_instructions=ctx.settings.get("conversation.global_instructions"),
                max_tool_iterations=(
                    conv_settings.get("max_tool_iterations", 25)
                    if isinstance(conv_settings, dict)
                    else 25
                ),
                max_tool_selections=(
                    conv_settings.get("max_tool_selections", 30)
                    if isinstance(conv_settings, dict)
                    else 30
                ),
                max_tool_result_tokens=(
                    conv_settings.get("max_tool_result_tokens", 4000)
                    if isinstance(conv_settings, dict)
                    else 4000
                ),
                rollup_threshold=(
                    conv_settings.get("rollup_threshold", 0.3)
                    if isinstance(conv_settings, dict)
                    else 0.3
                ),
                rollup_summary_ratio=(
                    conv_settings.get("rollup_summary_ratio", 0.3)
                    if isinstance(conv_settings, dict)
                    else 0.3
                ),
                emergency_rollup_threshold=(
                    conv_settings.get("emergency_rollup_threshold", 0.95)
                    if isinstance(conv_settings, dict)
                    else 0.95
                ),
                embedded_tools_config=embedded_tools_config,
                mcp_manager=mcp_manager,
                user_guid=app.state.user_guid,
                mcp_loop=getattr(app.state, "_mcp_loop", None),
                prompt_caching=bool(ctx.settings.get("conversation.prompt_caching", True)),
            )

            # Migrate any orphaned memories stored under "default" user_guid
            if app.state.user_guid != "default":
                try:
                    ph = database.connection.placeholder
                    cursor = database.connection.execute(
                        f"SELECT COUNT(*) FROM user_memories WHERE user_guid = {ph}",
                        ("default",),
                    )
                    count = cursor.fetchone()[0]
                    if count > 0:
                        database.connection.execute(
                            f"UPDATE user_memories SET user_guid = {ph} WHERE user_guid = {ph}",
                            (app.state.user_guid, "default"),
                        )
                        database.connection.commit()
                        logger.info(
                            "Migrated %d memories from 'default' to user %s",
                            count,
                            app.state.user_guid[:8] + "...",
                        )
                except Exception as e:
                    logger.debug("Memory migration check: %s", e)

            # Step 4: Embedding model (warm up)
            status["stage"] = "Loading embedding model..."
            try:
                from spark.index.embeddings import EmbeddingModel

                embedder = EmbeddingModel()
                embedder.encode("warmup")
                logger.info("Embedding model loaded")
            except Exception as e:
                logger.warning("Embedding model warmup failed (non-fatal): %s", e)

            # Step 5: Start daemon tray if enabled
            if ctx.settings.get("autonomous_actions.enabled") and ctx.settings.get(
                "daemon.enabled"
            ):
                status["stage"] = "Starting autonomous action daemon..."
                try:
                    _start_tray_daemon_if_needed()
                except Exception as e:
                    logger.warning("Daemon tray start failed (non-fatal): %s", e)

            status["stage"] = "Ready"
            status["ready"] = True
            logger.info("Background initialisation complete")

            # Step 6: Check for updates (non-blocking, after ready)
            try:
                from spark.core.updater import check_for_update

                include_pre = "a" in spark.__version__ or "b" in spark.__version__
                update_info = check_for_update(include_prereleases=include_pre)
                app.state.update_info = update_info
                if update_info.available:
                    logger.info(
                        "Update available: %s -> %s",
                        update_info.current_version,
                        update_info.latest_version,
                    )
            except Exception as e:
                logger.debug("Update check failed (non-fatal): %s", e)

            # Step 7: Start heartbeat monitor
            hb_enabled = ctx.settings.get("interface.browser_heartbeat.enabled", True)
            if hb_enabled:
                hb_interval = int(
                    ctx.settings.get("interface.browser_heartbeat.interval_seconds", 30) or 30
                )
                hb_misses = int(
                    ctx.settings.get("interface.browser_heartbeat.miss_threshold", 3) or 3
                )
                _start_heartbeat_monitor(app, hb_interval, hb_misses)

        except Exception as e:
            logger.error("Background init failed: %s", e)
            status["stage"] = f"Error: {e}"
            status["error"] = True

    thread = threading.Thread(target=_init, daemon=True)
    thread.start()


def _start_heartbeat_monitor(app: FastAPI, interval: int, max_misses: int) -> None:
    """Monitor browser heartbeat and shutdown if too many misses."""
    import signal
    import threading
    import time as _time

    app.state._last_heartbeat = _time.time()

    def _monitor() -> None:
        while True:
            _time.sleep(interval)
            last = getattr(app.state, "_last_heartbeat", _time.time())
            elapsed = _time.time() - last
            missed = int(elapsed / interval)
            if missed >= max_misses:
                logger.info(
                    "Browser heartbeat missed %d times (threshold %d) — shutting down",
                    missed,
                    max_misses,
                )
                os.kill(os.getpid(), signal.SIGINT)
                break

    monitor = threading.Thread(target=_monitor, daemon=True)
    monitor.start()
    logger.info("Heartbeat monitor started (interval=%ds, max_misses=%d)", interval, max_misses)


async def create_and_serve(ctx: AppContext, *, first_run: bool = False) -> None:
    """Create the app and run uvicorn."""
    app = create_app(ctx, first_run=first_run)

    # Set up init status tracking and start background init
    app.state.init_status = {"ready": False, "error": False, "stage": "Starting..."}
    _background_init(app, ctx)

    host = ctx.settings.get("interface.host", "127.0.0.1")
    port = _find_free_port(host)
    ssl_enabled = ctx.settings.get("interface.ssl.enabled", False)

    # Generate auth code and build auto-login URL
    auth_code = app.state.auth.generate_code()
    scheme = "https" if ssl_enabled else "http"
    url = f"{scheme}://{host}:{port}"
    login_url = f"{url}/auto-login?code={auth_code}"

    logger.info("Spark web interface starting at %s", url)
    logger.info("Authentication code: %s", auth_code)
    logger.debug("Pre-startup: writing URL file and scheduling browser open")

    # Write URL file so the tray daemon can find us
    from spark.core.application import _get_data_path

    url_file = _get_data_path() / "spark_url.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(url)

    # Open browser after a short delay (gives uvicorn time to bind the port)
    import threading

    def _open_browser_delayed() -> None:
        import time

        time.sleep(2)  # Simple delay — uvicorn binds quickly once serve() is called
        logger.debug("Opening browser at %s", login_url)
        webbrowser.open(login_url)

    threading.Thread(target=_open_browser_delayed, daemon=True).start()
    logger.debug("Browser open scheduled, configuring server...")

    # SSL configuration
    ssl_kwargs: dict = {}
    if ssl_enabled:
        cert_file = ctx.settings.get("interface.ssl.cert_file")
        key_file = ctx.settings.get("interface.ssl.key_file")
        auto_generate = ctx.settings.get("interface.ssl.auto_generate", False)

        if cert_file and key_file:
            ssl_kwargs["ssl_certfile"] = cert_file
            ssl_kwargs["ssl_keyfile"] = key_file
            logger.info("SSL enabled with certificate: %s", cert_file)
        elif auto_generate:
            try:
                from spark.web.ssl_utils import generate_self_signed_cert

                cert_path, key_path = generate_self_signed_cert()
                ssl_kwargs["ssl_certfile"] = str(cert_path)
                ssl_kwargs["ssl_keyfile"] = str(key_path)
                logger.info("SSL enabled with auto-generated certificate")
            except Exception as e:
                logger.warning("Failed to generate SSL certificate: %s", e)

    logger.debug("Creating uvicorn config...")
    try:
        loop = asyncio.get_event_loop()
        logger.info("Event loop type: %s", type(loop).__name__)
    except Exception as e:
        logger.error("Failed to get event loop: %s", e)

    try:
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            **ssl_kwargs,
        )
        logger.info("Uvicorn config created successfully")
    except Exception as e:
        logger.error("Failed to create uvicorn config: %s", e, exc_info=True)
        raise

    logger.info("Starting uvicorn server on %s:%d", host, port)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except Exception as e:
        logger.error("Uvicorn server failed to start: %s", e, exc_info=True)
        raise
    finally:
        # Clean up URL file on shutdown
        url_file.unlink(missing_ok=True)
