"""FastAPI web server for Spark."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import HTTPConnection

import spark
import spark.instance_settings as instance_settings
from spark.db import Database, SyncDatabase
from spark.instance_settings import consumers
from spark.mcp_integration import access as mcp_access
from spark.mode import DatabaseSettings
from spark.web.auth import AuthManager, GatingAuthenticator, LoginPolicy, PersonalAuthenticator
from spark.web.security import OriginPolicy, RequestGuardMiddleware
from spark.web.session import SessionManager

if TYPE_CHECKING:
    from konfig import AppContext

    from spark.identity.runtime import IdentityRuntime
    from spark.llm.manager import LLMManager
    from spark.mcp_integration.runtime import MCPRuntime

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

_DATABASE_WAIT_SECONDS = 300

# Personal mode: reachable without a session. Anything else is redirected to
# /login. Server mode's public paths are warden's (spark.identity.web).
_PUBLIC_PATHS = frozenset({"/login", "/auto-login", "/api/auth", "/loading", "/favicon.ico"})
_PUBLIC_PREFIXES = ("/static/", "/loading/")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _login_policy(ctx: Any) -> LoginPolicy:
    """Login code lifetime and lock-out settings (``interface.login.*``)."""
    defaults = LoginPolicy()
    get = ctx.settings.get
    return LoginPolicy(
        code_ttl_seconds=get(
            "interface.login.code_ttl_seconds", defaults.code_ttl_seconds, cast=float
        ),
        max_failures=get("interface.login.max_failures", defaults.max_failures, cast=int),
        failure_window_seconds=get(
            "interface.login.failure_window_seconds", defaults.failure_window_seconds, cast=float
        ),
        lockout_seconds=get(
            "interface.login.lockout_seconds", defaults.lockout_seconds, cast=float
        ),
    )


def _extra_origins(ctx: Any) -> list[str]:
    """Further origins allowed to make requests (``interface.allowed_origins``)."""
    value = ctx.settings.get("interface.allowed_origins", [])
    if isinstance(value, str):
        return [part for part in (p.strip() for p in value.split(",")) if part]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _initial_origin_policy(ctx: Any) -> OriginPolicy:
    """The instance's own origins as far as they are known before serving.

    With a fixed port they are known now; otherwise each request's Host header
    stands in until :func:`create_and_serve` has resolved the port.
    """
    port = ctx.settings.get("interface.port", 0)
    if isinstance(port, (int, str)) and str(port).isdigit() and int(port) > 0:
        scheme = "https" if ctx.settings.get("interface.ssl.enabled", False) else "http"
        host = str(ctx.settings.get("interface.host", "127.0.0.1"))
        return OriginPolicy.for_instance(scheme, host, int(port), _extra_origins(ctx))
    return OriginPolicy.same_host(_extra_origins(ctx))


def _template_csrf_token(request: Request) -> str:
    """The CSRF token for a page rendered for this request.

    The auth middleware sets it on signed-in requests; public pages (the
    loading page) ask the authenticator directly.
    """
    token = getattr(request.state, "csrf_token", None)
    if token is None:
        authenticator = getattr(request.app.state, "authenticator", None)
        token = authenticator.csrf_token(request) if authenticator is not None else None
    return str(token or "")


def attach_database(app: FastAPI, settings: DatabaseSettings, auto_migrate: bool) -> None:
    """Have the app's lifespan open this database (migrating first when ``auto_migrate``)."""
    app.state.database_settings = settings
    app.state.database_auto_migrate = auto_migrate


def attach_identity(app: FastAPI) -> None:
    """Sign users in through warden (server mode): call before serving, after ``attach_database``.

    Pins the Origin policy to ``server.public_base_url`` (plus
    ``interface.allowed_origins``), never the Host-header fallback, and makes
    warden the authenticator. Until the lifespan has started warden's runtime
    every request is refused (503). Raises
    :class:`spark.instance_settings.startup.StartupConfigError` without a valid
    public address.
    """
    from spark.identity.web import WardenAuthenticator
    from spark.instance_settings.startup import public_base_url

    ctx = app.state.ctx
    base_url = public_base_url(ctx.settings.get("server.public_base_url", ""))
    app.state.identity_attached = True
    app.state.authenticator = WardenAuthenticator()
    app.state.origin_policy = OriginPolicy.pinned(base_url, _extra_origins(ctx))


async def _start_identity(app: FastAPI, database: Database) -> IdentityRuntime | None:
    """Start warden's runtime when the app signs in through warden."""
    if not getattr(app.state, "identity_attached", False):
        return None
    from spark.identity.runtime import IdentityRuntime

    ctx = app.state.ctx
    runtime = await IdentityRuntime.start(database, ctx.settings, ctx.secrets)
    runtime.identity.install(app)
    app.state.identity_runtime = runtime
    return runtime


def _csrf_exempt(conn: HTTPConnection) -> bool:
    """Whether the authenticator says this request carries no ambient credential."""
    authenticator = conn.app.state.authenticator
    return isinstance(authenticator, GatingAuthenticator) and authenticator.csrf_exempt(conn)


async def _attach_settings(app: FastAPI, database: Database) -> None:
    """Install the settings source for the mode; in server mode start the settings listener.

    Nothing is installed when no mode was configured (an app built without
    :func:`create_and_serve`, as some tests do).
    """
    from spark import mode

    try:
        mode.current()
    except RuntimeError:
        return
    await instance_settings.attach(app, database, app.state.ctx.settings)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the database on the serving event loop, and close it on shutdown.

    Endpoints await ``app.state.database``; code on worker threads uses
    ``app.state.sync_db``. ``app.state.database_ready`` is set once either is
    usable or opening failed (``app.state.database_error``).

    With warden attached (server mode), warden's runtime starts once the
    database is open, runs for the app's lifetime with SPARK's audit hooks
    installed (:mod:`spark.identity.audit`), and stops before the database
    closes.
    """
    from spark.db import migrate
    from spark.identity import migrations as identity_migrations

    # warden's chain is migrated and checked with SPARK's (PostgreSQL only).
    identity_migrations.register()
    settings: DatabaseSettings | None = getattr(app.state, "database_settings", None)
    ready: threading.Event = app.state.database_ready
    database: Database | None = None
    identity: IdentityRuntime | None = None
    if settings is not None:
        try:
            await asyncio.to_thread(
                migrate.prepare,
                settings,
                auto_migrate=bool(getattr(app.state, "database_auto_migrate", False)),
            )
            database = await Database.open(settings)
        except BaseException as exc:
            app.state.database_error = exc
            ready.set()
            raise
        app.state.database = database
        app.state.sync_db = SyncDatabase(database)
        try:
            await _attach_settings(app, database)
            identity = await _start_identity(app, database)
        except BaseException as exc:
            app.state.database_error = exc
            ready.set()
            await instance_settings.detach(app)
            await database.close()
            raise
    ready.set()
    try:
        if identity is None:
            yield
        else:
            from spark.identity import audit as spark_audit

            assert database is not None
            # SPARK's audit hooks send its own actions to warden while warden runs.
            async with identity.running(), spark_audit.installed(database):
                app.state.authenticator.bind(identity)
                try:
                    yield
                finally:
                    app.state.authenticator.bind(None)
                    app.state.identity_runtime = None
    finally:
        mcp_runtime = getattr(app.state, "mcp_runtime", None)
        if mcp_runtime is not None:
            await asyncio.to_thread(mcp_runtime.stop)
            app.state.mcp_runtime = None
        if database is not None:
            await instance_settings.detach(app)
            app.state.sync_db = None
            app.state.database = None
            await database.close()


def _wait_for_database(app: FastAPI) -> SyncDatabase:
    """Block a start-up thread until the lifespan has opened the database."""
    if not app.state.database_ready.wait(_DATABASE_WAIT_SECONDS):
        raise RuntimeError("the database was not opened in time")
    error = getattr(app.state, "database_error", None)
    if error is not None:
        raise RuntimeError(f"the database could not be opened: {error}") from error
    sync_db: SyncDatabase | None = getattr(app.state, "sync_db", None)
    if sync_db is None:
        raise RuntimeError("no database is configured")
    return sync_db


def _local_user_guid(ctx: AppContext) -> str | None:
    """The single local user's guid in personal mode; None on a server (never asked for)."""
    from spark import mode

    try:
        server = mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        server = False
    if server:
        return None
    from spark.core.user_guid import get_user_guid

    return get_user_guid(ctx)


def create_app(ctx: AppContext, *, first_run: bool = False) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Spark",
        version=spark.__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.database = None
    app.state.sync_db = None
    app.state.database_error = None
    app.state.database_ready = threading.Event()

    # -- Shared state ---------------------------------------------------------
    timeout = ctx.settings.get("interface.session_timeout_minutes", 60, cast=int)
    max_age = ctx.settings.get("interface.session_max_age_hours", 24, cast=float)
    auth_manager = AuthManager(_login_policy(ctx))
    session_manager = SessionManager(timeout_minutes=timeout, max_age_hours=max_age)

    app.state.ctx = ctx
    app.state.auth = auth_manager
    app.state.session = session_manager
    # The web layer's only view of sign-in; server mode swaps in warden here.
    app.state.authenticator = PersonalAuthenticator(session_manager)
    # A server answers "may this user change instance settings?" from the
    # signed-in user's warden permissions, never the single-operator stub (D32).
    from spark.identity import access as identity_access

    identity_access.install()
    app.state.origin_policy = _initial_origin_policy(ctx)
    app.state.first_run = first_run
    app.state.init_status = {"ready": True, "error": False, "stage": "Ready"}
    # Personal mode's single local user. A server has no user of its own: every
    # request acts for its signed-in user, and the worker for each action's owner.
    app.state.user_guid = _local_user_guid(ctx)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.globals["version"] = spark.__version__
    templates.env.globals["csrf_token"] = _template_csrf_token
    # The Account menu's entries for the signed-in user's roles (server mode only).
    from spark.web.menu import identity_menu

    templates.env.globals["identity_menu"] = identity_menu
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

    # -- Origin and CSRF checks (inside the auth middleware) ---------------------
    app.add_middleware(
        RequestGuardMiddleware,
        policy=lambda conn: conn.app.state.origin_policy,
        csrf_token=lambda conn: conn.app.state.authenticator.csrf_token(conn),
        stream_prefixes=("/stream/",),
        csrf_exempt=_csrf_exempt,
    )

    # -- Auth middleware -------------------------------------------------------
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        authenticator = request.app.state.authenticator
        if isinstance(authenticator, GatingAuthenticator):
            # Server mode: warden decides, including which paths are public.
            answer = await authenticator.admit(request)
            if answer is not None:
                return answer
            # The MCP servers granted to the user's roles (instance settings, A12).
            mcp_access.apply_grants(request)
            request.state.csrf_token = authenticator.csrf_token(request) or ""
            return await call_next(request)

        if _is_public(request.url.path):
            return await call_next(request)

        if not authenticator.is_authenticated(request):
            return RedirectResponse(url="/login", status_code=303)

        request.state.csrf_token = authenticator.csrf_token(request) or ""
        return await call_next(request)

    return app


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Find a random available TCP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _resolve_port(ctx: Any, host: str = "127.0.0.1") -> int:
    """The configured fixed port, or a random free one (the default)."""
    configured = ctx.settings.get("interface.port", 0, cast=int)
    if configured and configured > 0:
        return int(configured)
    return _find_free_port(host)


def _should_open_browser(ctx: Any) -> bool:
    """Whether to auto-open the browser at startup (default: yes).

    Read as a raw value rather than cast=bool: an env override
    (SPARK__INTERFACE__OPEN_BROWSER=false) arrives as the string "false", which
    bool() would wrongly treat as truthy.
    """
    value = ctx.settings.get("interface.open_browser", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _resolve_secret(ctx: AppContext, value: str | None) -> str:
    """Resolve a secret:// URI via the konfig secrets backend, or return as-is."""
    if not value:
        return ""
    if hasattr(ctx, "secrets") and ctx.secrets:
        resolved = ctx.secrets.resolve_uri(value)
        return str(resolved) if resolved else ""
    return str(value)


def _init_providers(ctx: AppContext, settings: Any = None) -> "LLMManager":
    """Initialise LLM providers from settings and return an LLMManager.

    ``settings`` defaults to konfig (``config.yaml``); server mode passes the
    instance settings view.
    """
    from spark.llm.manager import LLMManager

    mgr = LLMManager()
    for provider in _build_providers(ctx, settings if settings is not None else ctx.settings):
        mgr.register_provider(provider)
    if mgr.providers:
        logger.info("LLM providers ready: %s", ", ".join(mgr.providers.keys()))
    else:
        logger.warning("No LLM providers configured")
    return mgr


def _build_providers(ctx: AppContext, settings: Any) -> list[Any]:
    """The enabled LLM providers described by ``settings``."""
    providers: list[Any] = []

    # Anthropic Direct
    if settings.get("providers.anthropic.enabled"):
        try:
            from spark.llm.anthropic_direct import AnthropicDirectProvider

            api_key = _resolve_secret(ctx, settings.get("providers.anthropic.api_key", ""))
            if api_key:
                provider = AnthropicDirectProvider(api_key=api_key)
                providers.append(provider)
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
            read_timeout = int(settings.get("providers.aws_bedrock.read_timeout", 300) or 300)

            # Only pass explicit keys when auth method is not SSO.
            if auth_method in ("iam", "session") and access_key and secret_key:
                provider = BedrockProvider(
                    region=region,
                    access_key=access_key,
                    secret_key=secret_key,
                    session_token=session_token or None,
                    read_timeout=read_timeout,
                )
            else:
                provider = BedrockProvider(
                    region=region, profile=profile, read_timeout=read_timeout
                )
            providers.append(provider)
        except Exception as e:
            logger.warning("Failed to init AWS Bedrock provider: %s", e)

    # Ollama
    if settings.get("providers.ollama.enabled"):
        try:
            from spark.llm.ollama import OllamaProvider

            base_url = settings.get("providers.ollama.base_url", "http://localhost:11434")
            provider = OllamaProvider(base_url=base_url)
            providers.append(provider)
        except Exception as e:
            logger.warning("Failed to init Ollama provider: %s", e)

    # Google Gemini
    if settings.get("providers.google_gemini.enabled"):
        try:
            from spark.llm.google_gemini import GoogleGeminiProvider

            api_key = _resolve_secret(ctx, settings.get("providers.google_gemini.api_key", ""))
            if api_key:
                provider = GoogleGeminiProvider(api_key=api_key)
                providers.append(provider)
        except Exception as e:
            logger.warning("Failed to init Google Gemini provider: %s", e)

    # X.AI
    if settings.get("providers.xai.enabled"):
        try:
            from spark.llm.xai import XAIProvider

            api_key = _resolve_secret(ctx, settings.get("providers.xai.api_key", ""))
            if api_key:
                provider = XAIProvider(api_key=api_key)
                providers.append(provider)
        except Exception as e:
            logger.warning("Failed to init X.AI provider: %s", e)

    return providers


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
            from spark.core.processes import pid_alive

            if not pid_alive(pid):
                raise OSError("not running")
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


def _warm_up_embeddings() -> None:
    """Load the local embedding model once, so the first search does not wait for it."""
    try:
        from spark.index.embeddings import SentenceTransformerEmbedding

        SentenceTransformerEmbedding().encode("warmup")
        logger.info("Embedding model loaded")
    except Exception as e:
        logger.warning("Embedding model warmup failed (non-fatal): %s", e)


def _start_daemon_if_enabled(ctx: AppContext, status: dict[str, Any]) -> None:
    """Start the tray daemon when actions and the daemon are enabled, in personal mode only.

    A server runs scheduled actions in ``spark worker``; its web process never
    starts the tray daemon.
    """
    from spark import mode

    if mode.is_server():
        return
    if not (ctx.settings.get("autonomous_actions.enabled") and ctx.settings.get("daemon.enabled")):
        return
    status["stage"] = "Starting autonomous action daemon..."
    try:
        _start_tray_daemon_if_needed()
    except Exception as e:
        logger.warning("Daemon tray start failed (non-fatal): %s", e)


def _personal_mcp_config() -> dict[str, Any]:
    """``config.yaml``'s contents for the MCP servers (personal mode)."""
    import yaml

    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    if not config_path.exists():
        return {}
    raw = yaml.safe_load(config_path.read_text()) or {}
    return raw if isinstance(raw, dict) else {}


def _start_mcp(
    app: FastAPI, ctx: AppContext, instance: _ServerConsumers | None
) -> MCPRuntime | None:
    """Connect the MCP servers on their own loop thread; never fails start-up.

    Server mode reads ``mcp.servers`` from the instance settings (credentials from
    the secrets store), never ``config.yaml``, and always starts the loop so a
    server an administrator adds later connects without a restart. Personal mode
    reads ``config.yaml`` and starts the loop only when servers are configured.
    """
    from spark.mcp_integration.manager import MCPManager
    from spark.mcp_integration.runtime import MCPRuntime

    runtime: MCPRuntime | None = None
    app.state.mcp_manager = None
    try:
        if instance is not None:
            manager = MCPManager()
            for config in consumers.mcp_server_configs(instance.source, instance.mcp_secret):
                manager.add_server(config)
            if (_personal_mcp_config().get("mcp") or {}).get("servers"):
                logger.warning(
                    "Ignoring config.yaml's mcp section: server mode reads mcp.servers "
                    "from the instance settings"
                )
        else:
            raw_config = _personal_mcp_config()
            if not (raw_config.get("mcp") or {}).get("servers"):
                return None
            manager = MCPManager.from_config(raw_config)
        runtime = MCPRuntime.start(manager)
        app.state.mcp_runtime = runtime
        app.state._mcp_loop = runtime.loop  # turn threads dispatch MCP calls onto it
        app.state.mcp_manager = manager
        runtime.connect_all()
    except Exception as e:
        logger.warning("MCP server init failed (non-fatal): %s", e)
    return runtime


class _ServerConsumers:
    """Server mode: the instance settings and secrets the web process's consumers read."""

    def __init__(self, ctx: AppContext, source: Any, secrets: Any) -> None:
        self._ctx = ctx
        self.source = source
        self.secrets = secrets
        self.view = consumers.InstanceView(source, secrets)

    def conversation_settings(self) -> dict[str, Any]:
        return {
            key.split(".", 1)[1]: self.source.get(key)
            for key in (
                "conversation.max_tool_iterations",
                "conversation.max_tool_selections",
                "conversation.max_tool_result_tokens",
                "conversation.rollup_threshold",
                "conversation.rollup_summary_ratio",
                "conversation.emergency_rollup_threshold",
            )
        }

    def embedded_tools_config(self) -> dict[str, Any]:
        return consumers.embedded_tools_config(self.source, self.secrets)

    def mcp_secret(self, name: str) -> str | None:
        """One MCP server credential from konfig's secrets store (server side only)."""
        store = getattr(self._ctx, "secrets", None)
        value = store.get(name) if store is not None else None
        return str(value) if value else None

    def follow(
        self, llm_manager: Any, conversation_manager: Any, mcp: MCPRuntime | None = None
    ) -> consumers.SettingsFollower:
        follower = consumers.follow(
            self.source,
            self.secrets,
            llm_manager=llm_manager,
            conversation_manager=conversation_manager,
            build_providers=lambda view: _build_providers(self._ctx, view),
            mcp=mcp,
            mcp_secrets=self.mcp_secret,
        )
        follower.changed()  # catch up with any change made while starting
        return follower


def _server_consumers(app: FastAPI, ctx: AppContext) -> _ServerConsumers | None:
    """The server-mode settings consumers, or ``None`` in personal mode."""
    from spark import mode
    from spark.instance_settings.secrets import SecretsWriter

    source = getattr(app.state, "instance_settings", None)
    if not mode.is_server() or not isinstance(source, instance_settings.ServerSettings):
        return None
    return _ServerConsumers(ctx, source, SecretsWriter(ctx.secrets, ctx.settings, server=True))


def _background_init(app: FastAPI, ctx: AppContext) -> None:
    """Heavy initialisation running in a background thread after server start."""
    import threading

    def _init() -> None:
        status = app.state.init_status
        try:
            # Step 1: Database (opened by the lifespan on the serving loop)
            status["stage"] = "Initialising database..."
            sync_db = _wait_for_database(app)
            # Server mode reads the administrator's instance settings, not config.yaml.
            instance = _server_consumers(app, ctx)

            # Step 2: LLM Providers
            status["stage"] = "Connecting to LLM providers..."
            llm_manager = _init_providers(ctx, instance.view if instance else None)
            app.state.llm_manager = llm_manager

            # Step 3: Conversation Manager
            status["stage"] = "Setting up conversation manager..."
            from spark.core.conversation_manager import ConversationManager
            from spark.llm.context_limits import ContextLimitResolver

            context_limits = ContextLimitResolver(
                ctx.settings.get("context_limits") if ctx.settings.get("context_limits") else None
            )
            conv_settings = (
                instance.conversation_settings()
                if instance
                else ctx.settings.get("conversation") or {}
            )
            embedded_tools_config = (
                instance.embedded_tools_config()
                if instance
                else {"embedded_tools": ctx.settings.get("embedded_tools") or {}}
            )

            # Resolve secret:// URIs within embedded tools config
            for _cat, cat_config in embedded_tools_config.get("embedded_tools", {}).items():
                if isinstance(cat_config, dict):
                    for key, val in cat_config.items():
                        if isinstance(val, str) and val.startswith("secret://"):
                            cat_config[key] = _resolve_secret(ctx, val)

            # Pass prompt inspection settings into the config (server mode: already there)
            if not instance and ctx.settings.get("prompt_inspection.enabled"):
                embedded_tools_config["_prompt_inspection_enabled"] = True
                embedded_tools_config["_prompt_inspection_level"] = ctx.settings.get(
                    "prompt_inspection.level", "standard"
                )
                embedded_tools_config["_prompt_inspection_action"] = ctx.settings.get(
                    "prompt_inspection.action", "warn"
                )

            # Step 3b: Connect saved MCP servers (server mode: the instance settings)
            status["stage"] = "Connecting MCP servers..."
            mcp_runtime = _start_mcp(app, ctx, instance)
            mcp_manager = mcp_runtime.manager if mcp_runtime is not None else None

            # Skills manager (content on disk; enable state in the database)
            try:
                from spark.skills.manager import get_skills_manager

                app.state.skills_manager = get_skills_manager()
            except Exception as e:  # noqa: BLE001 - skills must never block startup
                logger.warning("Skills unavailable this session: %s", e)
                app.state.skills_manager = None

            app.state.conversation_manager = ConversationManager(
                sync_db,
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
                # Personal mode's local user; a server's turns always name their user.
                user_guid=app.state.user_guid or "default",
                mcp_loop=getattr(app.state, "_mcp_loop", None),
                prompt_caching=bool(
                    instance.view.get("conversation.prompt_caching", True)
                    if instance
                    else ctx.settings.get("conversation.prompt_caching", True)
                ),
            )
            if instance:
                app.state.settings_follower = instance.follow(
                    llm_manager, app.state.conversation_manager, mcp_runtime
                )

            # Migrate any orphaned memories stored under "default" user_guid
            # (personal mode only: a server has no process user to give them to)
            if app.state.user_guid and app.state.user_guid != "default":
                try:
                    from spark.database import memories as memories_db

                    moved = sync_db.call(memories_db.reassign_user, "default", app.state.user_guid)
                    if moved:
                        logger.info(
                            "Migrated %d memories from 'default' to user %s",
                            moved,
                            app.state.user_guid[:8] + "...",
                        )
                except Exception as e:
                    logger.debug("Memory migration check: %s", e)

            # Step 4: Embedding model (warm up)
            status["stage"] = "Loading embedding model..."
            _warm_up_embeddings()

            # Step 5: Start daemon tray if enabled (personal mode only)
            _start_daemon_if_enabled(ctx, status)

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

    from spark import mode

    runtime = mode.current()
    server_mode = runtime.mode is mode.Mode.SERVER
    attach_database(app, runtime.database, auto_migrate=not server_mode)
    if server_mode:
        attach_identity(app)

    # Set up init status tracking and start background init
    app.state.init_status = {"ready": False, "error": False, "stage": "Starting..."}
    _background_init(app, ctx)

    host = ctx.settings.get("interface.host", "127.0.0.1")
    port = _resolve_port(ctx, host)
    ssl_enabled = ctx.settings.get("interface.ssl.enabled", False)

    scheme = "https" if ssl_enabled else "http"
    url = f"{scheme}://{host}:{port}"
    if server_mode:
        # Users sign in through warden at the public address; no login code.
        login_url = ""
        logger.info(
            "Spark server listening on %s, serving %s",
            url,
            ctx.settings.get("server.public_base_url"),
        )
    else:
        # Generate auth code and build auto-login URL
        auth_code = app.state.auth.generate_code()
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
        logger.debug("Opening browser at %s/auto-login", url)
        webbrowser.open(login_url)

    if server_mode:
        logger.info("Browser auto-open is off in server mode")
    elif _should_open_browser(ctx):
        threading.Thread(target=_open_browser_delayed, daemon=True).start()
        logger.debug("Browser open scheduled, configuring server...")
    else:
        logger.info("Browser auto-open disabled (interface.open_browser=false)")

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
        else:
            # Auto-generate a self-signed certificate if no cert files provided
            try:
                from spark.web.ssl_utils import generate_self_signed_cert

                cert_path, key_path = generate_self_signed_cert()
                ssl_kwargs["ssl_certfile"] = str(cert_path)
                ssl_kwargs["ssl_keyfile"] = str(key_path)
                logger.info("SSL enabled with auto-generated self-signed certificate")
            except Exception as e:
                logger.warning("Failed to generate SSL certificate: %s — falling back to HTTP", e)
                ssl_enabled = False
                scheme = "http"

    # Only this instance's own pages may change state or open event streams. A
    # server keeps the public origin attach_identity pinned (D18; Q14 keeps the
    # bind-address fallback for personal mode only).
    if not server_mode:
        app.state.origin_policy = OriginPolicy.for_instance(scheme, host, port, _extra_origins(ctx))

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
