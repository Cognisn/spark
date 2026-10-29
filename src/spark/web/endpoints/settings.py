"""Settings endpoints: the settings page in personal mode, and in server mode the
administrator's instance settings and each user's preferences.

Personal mode keeps one page writing ``config.yaml``. Server mode writes instance
settings to the database (``/settings/api/instance``, administrators only) and
preferences per user (``/settings/api/preferences``). Every request is checked
against the settings registry before anything is written: an unknown key, a key
of the wrong scope or an invalid value refuses the whole request. Secrets go
only to konfig's secrets store and are reported as set or not set; no endpoint
returns a secret value.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any, Protocol

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import spark.instance_settings as instance_settings
from spark import mode
from spark.instance_settings import access, accessor, registry
from spark.instance_settings.registry import Scope, SettingsError
from spark.instance_settings.secrets import SecretsWriter, reference
from spark.web.db import database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings")

_LOCK_SECRET_KEY = "settings_lock_password"


def _offers_local_tools() -> bool:
    """Whether this mode offers the local-only tools (personal mode does, a server does not)."""
    from spark.tools.registry import LOCAL_ONLY_CATEGORIES, is_category_available

    return all(is_category_available(c) for c in LOCAL_ONLY_CATEGORIES)


def _local_tool_refusal() -> JSONResponse | None:
    """Refuse a local-only tool setting or helper when the server does not offer those tools."""
    if _offers_local_tools():
        return None
    return JSONResponse(
        {"detail": "System command, filesystem and email tools are not available in server mode."},
        status_code=403,
    )


def _touches_unavailable_tools(dotted_key: str) -> bool:
    """Whether saving ``dotted_key`` would write settings for an unavailable tool category."""
    from spark.tools.registry import is_category_available

    parts = dotted_key.split(".")
    if parts[0] != "embedded_tools":
        return False
    if len(parts) > 1 and parts[1] == "tool_results":
        # Result actions are an administrator's policy on a server.
        return not _offers_local_tools()
    if len(parts) == 1:
        # The whole embedded tools tree: it may carry any category.
        return not _offers_local_tools()
    return not is_category_available(parts[1])


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _is_locked(request: Request) -> bool:
    """Check if settings are password-locked."""
    ctx = getattr(request.app.state, "ctx", None)
    if not ctx or not hasattr(ctx, "secrets") or not ctx.secrets:
        return False
    stored = ctx.secrets.get(_LOCK_SECRET_KEY)
    return bool(stored)


def _verify_lock(request: Request, password: str) -> bool:
    """Verify the settings lock password."""
    ctx = request.app.state.ctx
    stored = ctx.secrets.get(_LOCK_SECRET_KEY)
    if not stored:
        return True
    return bool(_hash_password(password) == stored)


def _server() -> bool:
    """Server mode? (False when the mode was never configured, as in some tests.)"""
    try:
        return mode.is_server()
    except RuntimeError:
        return False


def _writer(request: Request) -> SecretsWriter:
    ctx = request.app.state.ctx
    return SecretsWriter(ctx.secrets, ctx.settings, server=_server())


def _actor(request: Request) -> str:
    return access.access_check().user_guid(request) or "operator"


def _is_admin(request: Request) -> bool:
    return access.access_check().is_administrator(request)


class _SettingsGetter(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


class _View:
    """What the page builders read: values for the page, and secrets only as set or not set."""

    def __init__(self, values: Callable[[str, Any], Any], status: dict[str, bool]) -> None:
        self._values = values
        self._status = status

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        if registry.is_known(key) and registry.spec(key).secret:
            return None
        return self._values(key, default)

    def is_secret_set(self, key: str) -> bool:
        return self._status.get(key, False)


def _source_value(source: accessor.SettingsSource) -> Callable[[str, Any], Any]:
    def value(key: str, default: Any) -> Any:
        if registry.is_known(key):
            return source.get(key)
        return default

    return value


def _filter_sections(sections: list[dict[str, Any]], scopes: set[Scope]) -> list[dict[str, Any]]:
    """Keep only fields whose registered scope is in ``scopes``; drop emptied groups."""
    kept: list[dict[str, Any]] = []
    for section in sections:
        groups = []
        for group in section["groups"]:
            fields = [
                f
                for f in group["fields"]
                if registry.is_known(f["key"]) and registry.spec(f["key"]).scope in scopes
            ]
            if fields:
                groups.append({**group, "fields": fields})
        if groups:
            kept.append({**section, "groups": groups})
    return kept


def _render(
    request: Request,
    *,
    page: str,
    sections: list[dict[str, Any]],
    tool_categories: list[dict[str, Any]],
    save_url: str,
) -> HTMLResponse:
    llm_manager = getattr(request.app.state, "llm_manager", None)
    available_models = llm_manager.list_all_models() if llm_manager else []
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sections": sections,
            "tool_categories": tool_categories,
            "available_models": available_models,
            "is_locked": page == "personal" and _is_locked(request),
            "page": page,
            "save_url": save_url,
            "is_admin": page != "personal" and _is_admin(request),
        },
    )
    return response


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request) -> Response:
    """Personal mode: the one settings page. Server mode: the administrator's instance settings."""
    ctx = request.app.state.ctx
    status = _writer(request).status()
    if not _server():
        view = _View(lambda key, default: ctx.settings.get(key, default), status)
        return _render(
            request,
            page="personal",
            sections=_build_sections(view),
            tool_categories=_build_tool_categories(view),
            save_url="/settings/api/save",
        )
    if not _is_admin(request):
        return RedirectResponse(url="/settings/preferences", status_code=303)
    source = await instance_settings.server_settings(request.app, database(request), ctx.settings)
    view = _View(_source_value(source), status)
    sections = _filter_sections(_build_sections(view), {Scope.INSTANCE})
    sections.append(_models_offered_section(view))
    return _render(
        request,
        page="instance",
        sections=sections,
        tool_categories=_build_tool_categories(view),
        save_url="/settings/api/instance",
    )


@router.get("/preferences", response_class=HTMLResponse)
async def preferences_page(request: Request) -> Response:
    """Server mode: the signed-in user's own preferences (A4)."""
    if not _server():
        return RedirectResponse(url="/settings", status_code=303)
    user = access.access_check().user_guid(request)
    prefs = await accessor.preferences(database(request), user)
    view = _View(lambda key, default: prefs.get(key, default), {})
    sections = [_appearance_section(view)]
    sections += _filter_sections(_build_sections(view), {Scope.USER})
    return _render(
        request,
        page="preferences",
        sections=sections,
        tool_categories=[],
        save_url="/settings/api/preferences",
    )


def _unavailable_tool_settings(data: Any) -> JSONResponse | None:
    """Refuse, as a whole and before anything is written, settings for tools this mode does not offer."""
    if isinstance(data, dict) and any(_touches_unavailable_tools(str(k)) for k in data):
        return _local_tool_refusal() or JSONResponse(
            {"detail": "These tools are not available."}, status_code=403
        )
    return None


def _refused(message: str, keys: list[str], status: int = 400) -> JSONResponse:
    return JSONResponse({"detail": message, "keys": keys}, status_code=status)


def _checked(data: Any, scopes: set[Scope]) -> tuple[dict[str, Any], dict[str, str]] | JSONResponse:
    """Validate a whole request: (plain values, secrets) or the refusal. Nothing is written here.

    An empty or null secret means "leave it as it is", as does a null value (an
    emptied number field).
    """
    if not isinstance(data, dict):
        return _refused("expected an object of settings", [])
    unknown = [str(k) for k in data if not isinstance(k, str) or not registry.is_known(k)]
    if unknown:
        return _refused("unknown settings: " + ", ".join(unknown), unknown)
    wrong = [k for k in data if registry.spec(k).scope not in scopes]
    if wrong:
        return _refused("these settings cannot be changed here: " + ", ".join(wrong), wrong)
    plain: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    for key, value in data.items():
        secret = registry.spec(key).secret
        if value is None or (secret and value == ""):
            continue
        if secret:
            if not isinstance(value, str) or not value.strip():
                return _refused(f"invalid value for {key}: a secret must be non-empty text", [key])
            secrets[key] = value
            continue
        try:
            plain[key] = registry.coerce(key, value)
        except SettingsError as exc:
            return _refused(str(exc), [key])
    return plain, secrets


@router.post("/api/instance")
async def save_instance_settings(request: Request) -> JSONResponse:
    """Server mode, administrators only: store instance settings and secrets."""
    if not _server():
        return _refused("personal mode saves settings with /settings/api/save", [], 409)
    if not _is_admin(request):
        return _refused("only an administrator can change instance settings", [], 403)
    data = await request.json()
    if refused := _unavailable_tool_settings(data):
        return refused
    checked = _checked(data, {Scope.INSTANCE})
    if isinstance(checked, JSONResponse):
        return checked
    plain, secrets = checked
    actor = _actor(request)
    writer = _writer(request)
    for key, value in secrets.items():
        writer.set(key, value, actor=actor)
    source = await instance_settings.server_settings(
        request.app, database(request), request.app.state.ctx.settings
    )
    for key, value in plain.items():
        await source.set(key, value, actor=actor)
    return JSONResponse({"status": "ok", "saved": sorted([*plain, *secrets])})


@router.post("/api/preferences")
async def save_preferences(request: Request) -> JSONResponse:
    """Server mode: store the signed-in user's preferences."""
    if not _server():
        return _refused("personal mode saves settings with /settings/api/save", [], 409)
    user = access.access_check().user_guid(request)
    if not user:
        return _refused("no signed-in user", [], 403)
    checked = _checked(await request.json(), {Scope.USER})
    if isinstance(checked, JSONResponse):
        return checked
    plain, _secrets = checked
    for key, value in plain.items():
        await accessor.set_preference(database(request), user, key, value)
    return JSONResponse({"status": "ok", "saved": sorted(plain)})


@router.get("/api/secrets")
async def secret_status(request: Request) -> JSONResponse:
    """Every secret as set or not set (never a value); administrators only in server mode."""
    if _server() and not _is_admin(request):
        return _refused("only an administrator can see secrets", [], 403)
    return JSONResponse(_writer(request).status())


@router.post("/api/secrets/clear")
async def clear_secret(request: Request) -> JSONResponse:
    """Remove one secret from the store; administrators only in server mode."""
    if _server() and not _is_admin(request):
        return _refused("only an administrator can change secrets", [], 403)
    data = await request.json()
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not registry.is_known(key) or not registry.spec(key).secret:
        return _refused("expected the key of a secret", [str(key)])
    _writer(request).clear(key, actor=_actor(request))
    return JSONResponse({"status": "ok", "key": key, "set": False})


@router.post("/api/lock")
async def set_lock(request: Request) -> JSONResponse:
    """Set or update the settings lock password."""
    data = await request.json()
    password = data.get("password", "").strip()
    current_password = data.get("current_password", "")

    ctx = request.app.state.ctx

    # If already locked, verify current password first
    if _is_locked(request):
        if not _verify_lock(request, current_password):
            return JSONResponse({"error": "Current password is incorrect"}, status_code=403)

    if not password:
        # Remove lock
        try:
            ctx.secrets.delete(_LOCK_SECRET_KEY)
        except Exception:
            pass
        return JSONResponse({"status": "ok", "locked": False})

    # Set new password
    ctx.secrets.set(_LOCK_SECRET_KEY, _hash_password(password))
    return JSONResponse({"status": "ok", "locked": True})


@router.post("/api/unlock")
async def unlock_settings(request: Request) -> JSONResponse:
    """Verify the lock password to gain access."""
    data = await request.json()
    password = data.get("password", "")

    if _verify_lock(request, password):
        # Store unlock in session (using a simple flag on app state per session)
        session_id = request.cookies.get("spark_session", "")
        unlocked: set[str] = getattr(request.app.state, "_settings_unlocked", set())
        unlocked.add(session_id)
        request.app.state._settings_unlocked = unlocked
        return JSONResponse({"status": "ok"})

    return JSONResponse({"error": "Incorrect password"}, status_code=403)


@router.get("/api/lock-status")
async def lock_status(request: Request) -> JSONResponse:
    """Check if settings are locked and whether current session has unlocked."""
    locked = _is_locked(request)
    session_id = request.cookies.get("spark_session", "")
    unlocked_sessions: set[str] = getattr(request.app.state, "_settings_unlocked", set())
    unlocked = session_id in unlocked_sessions

    return JSONResponse({"locked": locked, "unlocked": unlocked})


def _handle_daemon_settings(data: dict[str, Any], ctx: Any) -> None:
    """Start or stop the tray daemon based on settings changes."""
    import os

    daemon_keys = {"autonomous_actions.enabled", "daemon.enabled"}
    if not daemon_keys.intersection(data.keys()):
        return

    actions_enabled = ctx.settings.get("autonomous_actions.enabled", False)
    daemon_enabled = ctx.settings.get("daemon.enabled", False)

    if actions_enabled and daemon_enabled:
        # Start daemon if not running
        try:
            from spark.web.server import _start_tray_daemon_if_needed

            _start_tray_daemon_if_needed()
        except Exception as e:
            logger.warning("Failed to start daemon: %s", e)
    else:
        # Stop daemon if running
        try:
            from spark.core.application import _get_data_path

            pid_file = _get_data_path() / "spark_daemon_tray.pid"
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                import signal

                os.kill(pid, signal.SIGTERM)
                pid_file.unlink(missing_ok=True)
                logger.info("Tray daemon stopped (PID %d)", pid)
        except Exception as e:
            logger.debug("Daemon stop: %s", e)


@router.post("/api/test-email")
async def test_email(request: Request) -> JSONResponse:
    """Test the SMTP email connection and optionally send a test email."""
    if refused := _local_tool_refusal():
        return refused

    import smtplib
    import ssl as _ssl
    from email.mime.text import MIMEText

    ctx = request.app.state.ctx
    settings = ctx.settings

    host = settings.get("embedded_tools.email.host", "")
    port = int(settings.get("embedded_tools.email.port", 587))
    username = settings.get("embedded_tools.email.username", "")
    sender = settings.get("embedded_tools.email.sender", "")
    use_tls = settings.get("embedded_tools.email.use_tls", True)

    # Resolve password from secrets backend
    from spark.web.server import _resolve_secret

    raw_password = settings.get("embedded_tools.email.password", "")
    password = _resolve_secret(ctx, raw_password) if raw_password else ""

    # Normalise boolean (may be string from config)
    if isinstance(use_tls, str):
        use_tls = use_tls.lower() == "true"

    # Check if a test send was requested
    try:
        data = await request.json()
    except Exception:
        data = {}
    send_test = data.get("send_test", False)

    base = {
        "host": host,
        "port": port,
        "username": username,
        "sender": sender,
        "use_tls": use_tls,
    }

    if not host:
        return JSONResponse({**base, "status": "error", "message": "SMTP host is not configured."})

    try:
        server = smtplib.SMTP(host, port, timeout=15)
        if use_tls:
            context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = _ssl.TLSVersion.TLSv1_2
            context.load_default_certs()
            server.starttls(context=context)

        auth_msg = "no authentication"
        if username and password:
            server.login(username, password)
            auth_msg = f"authenticated as {username}"

        result_msg = f"Connected and {auth_msg}."

        # Send a test email if requested
        if send_test and sender:
            test_msg = MIMEText(
                "This is a test email from Spark to verify your SMTP configuration.\n\n"
                "If you received this, your email settings are working correctly.",
                "plain",
            )
            test_msg["From"] = sender
            test_msg["To"] = sender
            test_msg["Subject"] = "Spark — Email Configuration Test"
            server.sendmail(sender, [sender], test_msg.as_string())
            result_msg += f" Test email sent to {sender}."

        server.quit()
        return JSONResponse({**base, "status": "ok", "message": result_msg})

    except smtplib.SMTPAuthenticationError:
        return JSONResponse(
            {
                **base,
                "status": "error",
                "message": "Authentication failed. Check username and password.",
            }
        )
    except smtplib.SMTPConnectError as e:
        return JSONResponse(
            {
                **base,
                "status": "error",
                "message": f"Could not connect to {host}:{port} — {e}",
            }
        )
    except smtplib.SMTPRecipientsRefused as e:
        return JSONResponse(
            {
                **base,
                "status": "error",
                "message": f"Connected but test send failed — recipients refused: {e}",
            }
        )
    except smtplib.SMTPSenderRefused as e:
        return JSONResponse(
            {
                **base,
                "status": "error",
                "message": f"Connected but sender address rejected: {e}",
            }
        )
    except ConnectionRefusedError:
        return JSONResponse(
            {
                **base,
                "status": "error",
                "message": f"Connection refused at {host}:{port}. Check the host and port.",
            }
        )
    except TimeoutError:
        return JSONResponse(
            {
                **base,
                "status": "error",
                "message": f"Connection timed out connecting to {host}:{port}.",
            }
        )
    except Exception as e:
        return JSONResponse({**base, "status": "error", "message": str(e)})


@router.get("/api/browse-directory")
async def browse_directory(request: Request) -> JSONResponse:
    """List subdirectories at a given path for the folder browser.

    Restricted to the user's home directory tree to prevent path traversal.
    """
    if refused := _local_tool_refusal():
        return refused

    from pathlib import Path

    home = Path.home().resolve()
    path_str = request.query_params.get("path", "")
    if not path_str:
        path_str = str(home)

    target = Path(path_str).resolve()  # NOSONAR — validated below against home dir

    # Prevent path traversal outside the home directory
    if not str(target).startswith(str(home)):
        return JSONResponse({"error": "Access denied"}, status_code=403)

    if not target.is_dir():
        return JSONResponse({"error": "Not a directory"}, status_code=400)

    dirs = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        pass

    return JSONResponse(
        {
            "current": str(target),
            "parent": str(target.parent) if target != target.parent else None,
            "directories": dirs,
        }
    )


@router.post("/api/save")
async def save_settings(request: Request) -> JSONResponse:
    """Personal mode: persist settings to config.yaml and secrets to the keychain."""
    data = await request.json()
    if refused := _unavailable_tool_settings(data):
        return refused
    if _server():
        return _refused(
            "server mode keeps settings in the database: use /settings/api/instance "
            "or /settings/api/preferences",
            [],
            409,
        )
    checked = _checked(data, {Scope.INSTANCE, Scope.USER, Scope.STARTUP})
    if isinstance(checked, JSONResponse):
        return checked
    plain, secrets = checked

    from spark.core.application import _get_config_path

    config_path = _get_config_path()

    try:
        raw = (
            yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if config_path.exists()
            else {}
        )
        ctx = request.app.state.ctx
        writer = _writer(request)

        # Secrets to the keychain; config.yaml keeps only a secret:// reference
        for dotted_key, value in secrets.items():
            writer.set(dotted_key, value, actor=_actor(request))
            _set_nested(raw, dotted_key, reference(dotted_key))
            logger.info("Stored secret for %s", dotted_key)
        for dotted_key, value in plain.items():
            _set_nested(raw, dotted_key, value)

        with config_path.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(raw, fh, default_flow_style=False, sort_keys=False)

        # Reload settings in memory
        ctx.settings.reload()

        # Update conversation manager's embedded tools config so new tools
        # (e.g. email) are visible without a restart.
        conv_mgr = getattr(request.app.state, "conversation_manager", None)
        if conv_mgr:
            new_embedded = {"embedded_tools": ctx.settings.get("embedded_tools") or {}}
            # Resolve secret:// URIs that konfig doesn't resolve in subtree gets
            _resolve_embedded_secrets(new_embedded.get("embedded_tools", {}), ctx)
            # Preserve runtime-injected keys (memory index, prompt inspection, etc.)
            for k, v in conv_mgr._embedded_tools_config.items():
                if k.startswith("_"):
                    new_embedded[k] = v
            conv_mgr._embedded_tools_config = new_embedded
            # Invalidate cached tool definitions so they're rebuilt on next use
            conv_mgr._builtin_tools = None
            conv_mgr._builtin_tool_names = None

        # Clear first-run flag
        request.app.state.first_run = False

        # Handle daemon start/stop based on settings changes
        _handle_daemon_settings(plain, ctx)

        logger.info("Settings saved to %s", config_path)
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        logger.error("Failed to save settings: %s", type(exc).__name__)
        return JSONResponse({"detail": "the settings could not be saved"}, status_code=500)


def _set_nested(d: dict[str, Any], dotted_key: str, value: object) -> None:
    """Set a value in a nested dict using a dot-separated key."""
    keys = dotted_key.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _resolve_embedded_secrets(embedded: dict[str, Any], ctx: Any) -> None:
    """Resolve secret:// URIs in embedded tools config subtrees in-place."""
    from spark.web.server import _resolve_secret

    for _category, cat_config in embedded.items():
        if isinstance(cat_config, dict):
            for key, val in cat_config.items():
                if isinstance(val, str) and val.startswith("secret://"):
                    cat_config[key] = _resolve_secret(ctx, val)


def _build_sections(settings: _SettingsGetter) -> list[dict[str, Any]]:
    """Build settings sections for the template."""
    sections = [
        {
            "id": "providers",
            "title": "LLM Providers",
            "icon": "bi-robot",
            "description": "Configure your AI model providers.",
            "groups": [
                {
                    "id": "anthropic",
                    "title": "Anthropic",
                    "help_url": "/help/provider/anthropic",
                    "fields": [
                        _toggle("providers.anthropic.enabled", "Enabled", settings),
                        _secret("providers.anthropic.api_key", "API Key", settings),
                    ],
                },
                {
                    "id": "aws_bedrock",
                    "title": "AWS Bedrock",
                    "help_url": "/help/provider/aws_bedrock",
                    "fields": [
                        _toggle("providers.aws_bedrock.enabled", "Enabled", settings),
                        _text("providers.aws_bedrock.region", "Region", settings, "us-east-1"),
                        _select(
                            "providers.aws_bedrock.auth_method",
                            "Auth Method",
                            settings,
                            ["sso", "iam", "session"],
                            "sso",
                        ),
                        {
                            **_text(
                                "providers.aws_bedrock.profile",
                                "SSO Profile",
                                settings,
                                "",
                            ),
                            "bedrock_auth": "sso",
                            "help": "AWS CLI profile name. Leave empty to use the default profile.",
                        },
                        {
                            **_secret(
                                "providers.aws_bedrock.access_key",
                                "Access Key ID",
                                settings,
                            ),
                            "bedrock_auth": "keys",
                        },
                        {
                            **_secret(
                                "providers.aws_bedrock.secret_key",
                                "Secret Access Key",
                                settings,
                            ),
                            "bedrock_auth": "keys",
                        },
                        {
                            **_secret(
                                "providers.aws_bedrock.session_token",
                                "Session Token",
                                settings,
                            ),
                            "bedrock_auth": "session",
                            "help": "Required for temporary session credentials only.",
                        },
                        {
                            **_number(
                                "providers.aws_bedrock.read_timeout",
                                "Read Timeout (seconds)",
                                settings,
                                300,
                            ),
                            "help": "Maximum time to wait for a response from Bedrock. "
                            "Increase for large contexts or complex tool-use conversations.",
                        },
                    ],
                },
                {
                    "id": "ollama",
                    "title": "Ollama",
                    "help_url": "/help/provider/ollama",
                    "fields": [
                        _toggle("providers.ollama.enabled", "Enabled", settings),
                        _text(
                            "providers.ollama.base_url",
                            "Base URL",
                            settings,
                            "http://localhost:11434",
                        ),
                    ],
                },
                {
                    "id": "google_gemini",
                    "title": "Google Gemini",
                    "help_url": "/help/provider/google_gemini",
                    "fields": [
                        _toggle("providers.google_gemini.enabled", "Enabled", settings),
                        _secret("providers.google_gemini.api_key", "API Key", settings),
                    ],
                },
                {
                    "id": "xai",
                    "title": "X.AI",
                    "help_url": "/help/provider/xai",
                    "fields": [
                        _toggle("providers.xai.enabled", "Enabled", settings),
                        _secret("providers.xai.api_key", "API Key", settings),
                    ],
                },
            ],
        },
        {
            "id": "database",
            "title": "Database",
            "icon": "bi-database",
            "description": (
                "Personal mode keeps conversations and memory in a local SQLite database. "
                "Server mode needs a PostgreSQL URL. A change of mode applies on restart."
            ),
            "groups": [
                {
                    "id": "database",
                    "title": "Mode",
                    "fields": [
                        _select("mode", "Mode", settings, ["personal", "server"], "personal"),
                        _secret("database.url", "PostgreSQL URL (server mode)", settings),
                    ],
                },
            ],
        },
        {
            "id": "voice",
            "title": "Voice",
            "icon": "bi-mic",
            "description": (
                "Text-to-speech for voice mode. The browser synthesiser is used by "
                "default and remains the fallback whenever ElevenLabs is unavailable."
            ),
            "groups": [
                {
                    "id": "voice_engine",
                    "title": "Engine",
                    "fields": [
                        _select(
                            "voice.engine",
                            "Speech engine",
                            settings,
                            ["browser", "elevenlabs"],
                            "browser",
                        ),
                        _select(
                            "voice.interaction_mode",
                            "Debate and panel voice mode",
                            settings,
                            ["listen_along", "immersive", "listen_only"],
                            "listen_along",
                        ),
                    ],
                },
                {
                    "id": "elevenlabs",
                    "title": "ElevenLabs",
                    "fields": [
                        _secret("voice.elevenlabs.api_key", "API Key", settings),
                        _select(
                            "voice.elevenlabs.model_id",
                            "Model",
                            settings,
                            [
                                "eleven_flash_v2_5",
                                "eleven_multilingual_v2",
                                "eleven_v3",
                            ],
                            "eleven_flash_v2_5",
                        ),
                        _select(
                            "voice.elevenlabs.default_voice_id",
                            "Default voice",
                            settings,
                            [
                                str(
                                    _get_val(settings, "voice.elevenlabs.default_voice_id", "")
                                    or ""
                                )
                            ],
                            "",
                        ),
                        _number(
                            "voice.elevenlabs.monthly_character_cap",
                            "Monthly character cap (0 = unlimited)",
                            settings,
                            100000,
                        ),
                        _toggle("voice.elevenlabs.cache_enabled", "Cache audio", settings),
                        _number(
                            "voice.elevenlabs.cache_max_mb",
                            "Audio cache size (MB)",
                            settings,
                            200,
                        ),
                    ],
                },
            ],
        },
        {
            "id": "interface",
            "title": "Web Interface",
            "icon": "bi-globe",
            "description": "Web server and session settings.",
            "groups": [
                {
                    "id": "server",
                    "title": "Server",
                    "fields": [
                        _toggle("interface.ssl.enabled", "Enable SSL", settings),
                        _number(
                            "interface.session_timeout_minutes",
                            "Session Timeout (minutes)",
                            settings,
                            60,
                        ),
                    ],
                },
            ],
        },
        {
            "id": "conversation",
            "title": "Conversation",
            "icon": "bi-chat-dots",
            "description": "Global instructions, context management, and tool execution settings.",
            "groups": [
                {
                    "id": "global_instructions",
                    "title": "Global Instructions",
                    "fields": [
                        _textarea(
                            "conversation.global_instructions",
                            "System Instructions",
                            settings,
                            "",
                            rows=6,
                            help_text="These instructions are prepended to every conversation alongside the built-in system prompt and any per-conversation instructions.",
                        ),
                    ],
                },
                {
                    "id": "context",
                    "title": "Context Compaction",
                    "fields": [
                        _number(
                            "conversation.rollup_threshold",
                            "Rollup Threshold",
                            settings,
                            0.3,
                        ),
                        _number(
                            "conversation.rollup_summary_ratio",
                            "Summary Ratio",
                            settings,
                            0.3,
                        ),
                        _number(
                            "conversation.emergency_rollup_threshold",
                            "Emergency Threshold",
                            settings,
                            0.95,
                        ),
                    ],
                },
                {
                    "id": "prompt_caching",
                    "title": "Prompt Caching",
                    "fields": [
                        {
                            "type": "toggle",
                            "key": "conversation.prompt_caching",
                            "label": "Enable Prompt Caching",
                            "value": bool(_get_val(settings, "conversation.prompt_caching", True)),
                            "help": "Cache system prompts and tool definitions across requests to reduce input token costs. Supported by Anthropic. Default: on.",
                        },
                    ],
                },
                {
                    "id": "tools",
                    "title": "Tool Execution",
                    "fields": [
                        _number(
                            "conversation.max_tool_iterations",
                            "Max Tool Iterations",
                            settings,
                            25,
                        ),
                        _number(
                            "conversation.max_tool_selections",
                            "Max Tool Selections",
                            settings,
                            30,
                        ),
                        _number(
                            "conversation.max_tool_result_tokens",
                            "Max Result Tokens",
                            settings,
                            4000,
                        ),
                    ],
                },
            ],
        },
        {
            "id": "security",
            "title": "Security",
            "icon": "bi-shield-check",
            "description": "Prompt inspection and tool permissions.",
            "groups": [
                {
                    "id": "prompt_inspection",
                    "title": "Prompt Inspection",
                    "fields": [
                        _toggle("prompt_inspection.enabled", "Enabled", settings),
                        _select(
                            "prompt_inspection.level",
                            "Level",
                            settings,
                            ["basic", "standard", "strict"],
                            "standard",
                        ),
                        _select(
                            "prompt_inspection.action",
                            "Action",
                            settings,
                            ["block", "warn", "sanitize", "log_only"],
                            "warn",
                        ),
                    ],
                },
                {
                    "id": "tool_perms",
                    "title": "Tool Permissions",
                    "fields": [
                        _toggle("tool_permissions.auto_approve", "Auto-approve Tools", settings),
                    ],
                },
            ],
        },
        {
            "id": "logging",
            "title": "Logging",
            "icon": "bi-journal-text",
            "description": "Application logging configuration.",
            "groups": [
                {
                    "id": "logging",
                    "title": "Log Settings",
                    "fields": [
                        _select(
                            "logging.level",
                            "Level",
                            settings,
                            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                            "INFO",
                        ),
                        _select(
                            "logging.format",
                            "Format",
                            settings,
                            ["text", "json"],
                            "text",
                        ),
                        _number("logging.retention_runs", "Retention (runs)", settings, 10),
                    ],
                },
            ],
        },
        {
            "id": "default_model",
            "title": "Default Model",
            "icon": "bi-cpu",
            "description": "Set a default or mandatory model for new conversations.",
            "groups": [
                {
                    "id": "default_model",
                    "title": "Model Selection",
                    "fields": [
                        {
                            "type": "model_select",
                            "key": "default_model.model_id",
                            "label": "Default Model",
                            "value": _get_val(settings, "default_model.model_id", ""),
                        },
                        _select(
                            "default_model.mode",
                            "Mode",
                            settings,
                            ["default", "mandatory"],
                            "default",
                        ),
                    ],
                },
            ],
        },
        {
            "id": "agents",
            "title": "Agents",
            "icon": "bi-robot",
            "description": "Allow the AI to spawn sub-agents for independent task execution within conversations.",
            "groups": [
                {
                    "id": "agent_settings",
                    "title": "Agent Settings",
                    "fields": [
                        _toggle("embedded_tools.agents.enabled", "Enable Agent Spawning", settings),
                        _select(
                            "embedded_tools.agents.default_mode",
                            "Default Mode",
                            settings,
                            ["orchestrator", "chain"],
                            "orchestrator",
                        ),
                        _select(
                            "embedded_tools.agents.model_selection",
                            "Model Selection",
                            settings,
                            ["same", "auto_select", "auto_select_approved"],
                            "same",
                        ),
                        _number(
                            "embedded_tools.agents.max_concurrent",
                            "Max Concurrent Agents",
                            settings,
                            5,
                        ),
                        _number(
                            "embedded_tools.agents.max_iterations",
                            "Max Iterations per Agent",
                            settings,
                            15,
                        ),
                    ],
                },
            ],
        },
        {
            "id": "autonomous_actions",
            "title": "Autonomous Actions",
            "icon": "bi-play-circle",
            "description": "Configure the autonomous action execution system.",
            "groups": [
                {
                    "id": "actions",
                    "title": "Actions",
                    "fields": [
                        _toggle(
                            "autonomous_actions.enabled", "Enable Autonomous Actions", settings
                        ),
                        _number(
                            "autonomous_actions.max_concurrent",
                            "Max Concurrent Executions",
                            settings,
                            3,
                        ),
                    ],
                },
                {
                    "id": "daemon",
                    "title": "Daemon",
                    "fields": [
                        _toggle("daemon.enabled", "Enable Background Daemon", settings),
                        _number(
                            "daemon.heartbeat_interval",
                            "Heartbeat Interval (seconds)",
                            settings,
                            30,
                        ),
                    ],
                },
            ],
        },
    ]
    # Personal mode sets the action for flagged tool results here; a server
    # reads them from its settings keys (administrators edit them centrally).
    if _offers_local_tools():
        for section in sections:
            if section["id"] == "security":
                section["groups"].append(_tool_result_group(settings))
    return sections


_RESULT_SOURCES = (
    ("web", "Web search and fetch"),
    ("mcp", "MCP tools"),
    ("documents", "Documents and archives"),
    ("agents", "Sub-agents"),
)


def _tool_result_group(settings: object) -> dict:
    """Per-source action for tool results that match prompt injection patterns."""
    return {
        "id": "tool_results",
        "title": "Tool Result Inspection",
        "fields": [
            _select(
                f"embedded_tools.tool_results.{source}",
                label,
                settings,
                ["mark", "warn", "withhold"],
                "warn",
            )
            for source, label in _RESULT_SOURCES
        ],
    }


# -- Field helpers --


def _get_val(settings: _SettingsGetter, key: str, default: Any = None) -> Any:
    return settings.get(key, default)


def _ensure_list(value: object) -> list[Any]:
    """Ensure a value is a list of path strings.

    Handles: proper list, comma-separated string, None, and the corruption
    case where a path was split into individual characters (e.g.
    ['/', 'U', 's', ...] instead of ['/Users/...']).
    """
    if isinstance(value, list):
        # Detect character-split corruption: if most entries are single chars,
        # join them back into a single path string.
        if len(value) > 3 and all(isinstance(v, str) and len(v) <= 1 for v in value):
            rejoined = "".join(value)
            return [rejoined] if rejoined else []
        return value
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if value is None:
        return []
    return []


def _toggle(key: str, label: str, settings: _SettingsGetter) -> dict[str, Any]:
    return {"type": "toggle", "key": key, "label": label, "value": bool(_get_val(settings, key))}


def _text(key: str, label: str, settings: _SettingsGetter, default: object = "") -> dict[str, Any]:
    return {"type": "text", "key": key, "label": label, "value": _get_val(settings, key, default)}


def _number(
    key: str, label: str, settings: _SettingsGetter, default: object = ""
) -> dict[str, Any]:
    return {
        "type": "number",
        "key": key,
        "label": label,
        "value": _get_val(settings, key, default),
    }


def _textarea(
    key: str,
    label: str,
    settings: _SettingsGetter,
    default: str = "",
    rows: int = 4,
    help_text: str = "",
) -> dict[str, Any]:
    return {
        "type": "textarea",
        "key": key,
        "label": label,
        "value": _get_val(settings, key, default) or "",
        "rows": rows,
        "help": help_text,
    }


def _secret_is_set(settings: _SettingsGetter, key: str) -> bool:
    """Whether a secret is set; the value itself never reaches the page."""
    if isinstance(settings, _View):
        return settings.is_secret_set(key)
    val = _get_val(settings, key, "")
    return bool(val) and str(val) != ""


def _secret(key: str, label: str, settings: _SettingsGetter) -> dict[str, Any]:
    return {
        "type": "secret",
        "key": key,
        "label": label,
        "has_value": _secret_is_set(settings, key),
    }


def _models_offered_section(settings: _SettingsGetter) -> dict[str, Any]:
    """Server mode: which models users may choose (empty offers every model)."""
    offered = _get_val(settings, "models.offered", []) or []
    return {
        "id": "models_offered",
        "title": "Models Offered",
        "icon": "bi-list-check",
        "description": "The model ids users may choose from. Leave empty to offer every model.",
        "groups": [
            {
                "id": "models_offered",
                "title": "Models",
                "fields": [
                    {
                        "type": "text",
                        "key": "models.offered",
                        "label": "Model ids (comma-separated)",
                        "value": ", ".join(offered) if isinstance(offered, list) else "",
                    }
                ],
            }
        ],
    }


def _appearance_section(settings: _SettingsGetter) -> dict[str, Any]:
    """Server mode preferences: the user's theme."""
    return {
        "id": "appearance",
        "title": "Appearance",
        "icon": "bi-palette",
        "description": "How Spark looks for you.",
        "groups": [
            {
                "id": "appearance",
                "title": "Theme",
                "fields": [
                    _select("interface.theme", "Theme", settings, ["dark", "light"], "dark")
                ],
            }
        ],
    }


def _select(
    key: str, label: str, settings: _SettingsGetter, options: list[str], default: str = ""
) -> dict[str, Any]:
    return {
        "type": "select",
        "key": key,
        "label": label,
        "value": _get_val(settings, key, default),
        "options": options,
    }


def _build_tool_categories(settings: _SettingsGetter) -> list[dict[str, Any]]:
    """Build embedded tool category data for the tools management panel."""
    from spark.tools.registry import is_category_available

    get = settings.get

    categories = [
        {
            "id": "filesystem",
            "title": "Filesystem",
            "icon": "bi-folder2-open",
            "description": "Read, write, search, and inspect files and directories.",
            "enabled": bool(get("embedded_tools.filesystem.enabled", True)),
            "mode": get("embedded_tools.filesystem.mode", "read"),
            "mode_options": ["read", "read_write"],
            "mode_descriptions": {
                "read": "Read-only access to files within allowed paths.",
                "read_write": "Full read and write access. Use with caution.",
            },
            "extra_fields": [
                {
                    "key": "embedded_tools.filesystem.allowed_paths",
                    "label": "Allowed Paths",
                    "type": "path_list",
                    "value": _ensure_list(get("embedded_tools.filesystem.allowed_paths", [])),
                    "help": "Directories the AI can access. Empty = working directory only.",
                },
            ],
            "tools": [
                "read_file",
                "list_directory",
                "search_files",
                "get_file_info",
                "find_in_file",
                "get_directory_tree",
                "write_file",
            ],
        },
        {
            "id": "documents",
            "title": "Documents",
            "icon": "bi-file-earmark-richtext",
            "description": "Read Word, Excel, PowerPoint, and PDF documents.",
            "enabled": bool(get("embedded_tools.documents.enabled", True)),
            "mode": get("embedded_tools.documents.mode", "read"),
            "mode_options": ["read", "read_write"],
            "mode_descriptions": {
                "read": "Read document contents only.",
                "read_write": "Read and create documents.",
            },
            "extra_fields": [
                {
                    "key": "embedded_tools.documents.max_file_size_mb",
                    "label": "Max File Size (MB)",
                    "type": "number",
                    "value": get("embedded_tools.documents.max_file_size_mb", 50),
                    "help": "Maximum file size in megabytes for document reading.",
                },
            ],
            "tools": [
                "read_word",
                "read_excel",
                "read_pdf",
                "read_powerpoint",
                "create_word",
                "create_excel",
                "create_powerpoint",
                "create_pdf",
            ],
        },
        {
            "id": "archives",
            "title": "Archives",
            "icon": "bi-file-zip",
            "description": "List and extract ZIP and TAR archive contents.",
            "enabled": bool(get("embedded_tools.archives.enabled", True)),
            "mode": get("embedded_tools.archives.mode", "list"),
            "mode_options": ["list", "extract"],
            "mode_descriptions": {
                "list": "List archive contents only.",
                "extract": "List and extract archive files.",
            },
            "extra_fields": [],
            "tools": ["list_archive", "extract_archive"],
        },
        {
            "id": "web",
            "title": "Web",
            "icon": "bi-globe2",
            "description": "Search the web and fetch page content.",
            "enabled": bool(get("embedded_tools.web.enabled", True)),
            "mode": None,
            "mode_options": [],
            "mode_descriptions": {},
            "extra_fields": [
                {
                    "key": "embedded_tools.web.search_engine",
                    "label": "Search Engine",
                    "type": "select",
                    "value": get("embedded_tools.web.search_engine", "duckduckgo"),
                    "options": [
                        {"value": "duckduckgo", "label": "DuckDuckGo (no API key)"},
                        {"value": "brave", "label": "Brave Search"},
                        {"value": "google", "label": "Google (SerpAPI)"},
                        {"value": "bing", "label": "Bing (Azure)"},
                        {"value": "searxng", "label": "SearXNG (self-hosted)"},
                    ],
                    "help": "DuckDuckGo requires no API key. Other engines need credentials configured below.",
                },
                {
                    "key": "embedded_tools.web.brave_api_key",
                    "label": "Brave API Key",
                    "type": "secret",
                    "has_value": _secret_is_set(settings, "embedded_tools.web.brave_api_key"),
                    "help": "Free tier at search.brave.com/api",
                    "show_when": "brave",
                },
                {
                    "key": "embedded_tools.web.google_api_key",
                    "label": "SerpAPI Key",
                    "type": "secret",
                    "has_value": _secret_is_set(settings, "embedded_tools.web.google_api_key"),
                    "help": "Get a key at serpapi.com",
                    "show_when": "google",
                },
                {
                    "key": "embedded_tools.web.bing_api_key",
                    "label": "Bing API Key",
                    "type": "secret",
                    "has_value": _secret_is_set(settings, "embedded_tools.web.bing_api_key"),
                    "help": "Azure Cognitive Services Bing Search key",
                    "show_when": "bing",
                },
                {
                    "key": "embedded_tools.web.searxng_url",
                    "label": "SearXNG URL",
                    "type": "text",
                    "value": get("embedded_tools.web.searxng_url", ""),
                    "help": "Full URL of your SearXNG instance, e.g. https://search.example.com",
                    "show_when": "searxng",
                },
            ],
            "tools": ["web_search", "web_fetch"],
        },
        {
            "id": "system_commands",
            "title": "System Commands",
            "icon": "bi-terminal",
            "description": "Execute shell commands on the host system. The LLM can run CLI tools, scripts, and system utilities.",
            "enabled": bool(get("embedded_tools.system_commands.enabled", False)),
            "mode": None,
            "mode_options": [],
            "mode_descriptions": {},
            "extra_fields": [
                {
                    "key": "embedded_tools.system_commands.timeout",
                    "label": "Default Timeout (seconds)",
                    "type": "number",
                    "value": get("embedded_tools.system_commands.timeout", 30),
                    "help": "Maximum time a single command can run. Range: 1-300 seconds.",
                },
                {
                    "key": "embedded_tools.system_commands.max_output_chars",
                    "label": "Max Output (characters)",
                    "type": "number",
                    "value": get("embedded_tools.system_commands.max_output_chars", 50000),
                    "help": "Maximum output length returned to the LLM. Longer output is truncated.",
                },
                {
                    "key": "embedded_tools.system_commands.blocked_commands",
                    "label": "Blocked Commands",
                    "type": "text",
                    "value": ", ".join(
                        get("embedded_tools.system_commands.blocked_commands", []) or []
                    ),
                    "help": "Comma-separated list of command names to block (e.g. rm, shutdown, reboot). Some dangerous commands are always blocked.",
                },
                {
                    "key": "embedded_tools.system_commands.require_approval",
                    "label": "Always Require Approval",
                    "type": "select",
                    "value": str(
                        get("embedded_tools.system_commands.require_approval", True)
                    ).lower(),
                    "options": [
                        {"value": "true", "label": "Yes — prompt for every command"},
                        {
                            "value": "false",
                            "label": "No — respect tool permission settings",
                        },
                    ],
                    "help": "When enabled, every command requires user approval even if previously approved. Disable for autonomous actions.",
                },
            ],
            "tools": ["run_command"],
        },
        {
            "id": "email",
            "title": "Email",
            "icon": "bi-envelope",
            "description": "Send and draft emails via SMTP. Requires SMTP server configuration.",
            "enabled": bool(get("embedded_tools.email.enabled", False)),
            "mode": None,
            "mode_options": [],
            "mode_descriptions": {},
            "extra_fields": [
                {
                    "key": "embedded_tools.email.host",
                    "label": "SMTP Host",
                    "type": "text",
                    "value": get("embedded_tools.email.host", ""),
                    "help": "SMTP server hostname (e.g. smtp.gmail.com, smtp.office365.com).",
                },
                {
                    "key": "embedded_tools.email.port",
                    "label": "SMTP Port",
                    "type": "number",
                    "value": get("embedded_tools.email.port", 587),
                    "help": "587 for STARTTLS (recommended), 465 for SSL, 25 for unencrypted.",
                },
                {
                    "key": "embedded_tools.email.username",
                    "label": "Username",
                    "type": "text",
                    "value": get("embedded_tools.email.username", ""),
                    "help": "SMTP authentication username (usually your email address).",
                },
                {
                    "key": "embedded_tools.email.password",
                    "label": "Password",
                    "type": "secret",
                    "has_value": _secret_is_set(settings, "embedded_tools.email.password"),
                    "help": "SMTP password or app-specific password. Kept in the secrets store, never shown.",
                },
                {
                    "key": "embedded_tools.email.sender",
                    "label": "Sender Address",
                    "type": "text",
                    "value": get("embedded_tools.email.sender", ""),
                    "help": "The From address for outgoing emails.",
                },
                {
                    "key": "embedded_tools.email.use_tls",
                    "label": "Use TLS",
                    "type": "select",
                    "value": str(get("embedded_tools.email.use_tls", True)).lower(),
                    "options": [
                        {"value": "true", "label": "Yes (STARTTLS)"},
                        {"value": "false", "label": "No"},
                    ],
                    "help": "Enable STARTTLS encryption. Recommended for port 587.",
                },
                {
                    "key": "embedded_tools.email.max_attachment_mb",
                    "label": "Max Attachment Size (MB)",
                    "type": "number",
                    "value": get("embedded_tools.email.max_attachment_mb", 25),
                    "help": "Maximum total attachment size in megabytes.",
                },
                {
                    "key": "embedded_tools.email.require_approval",
                    "label": "Always Require Approval",
                    "type": "select",
                    "value": str(get("embedded_tools.email.require_approval", True)).lower(),
                    "options": [
                        {"value": "true", "label": "Yes — always prompt before sending"},
                        {"value": "false", "label": "No — respect tool permission settings"},
                    ],
                    "help": "When enabled, send_email always requires approval even if the tool has been globally approved. Disable for autonomous actions.",
                },
            ],
            "tools": ["send_email", "draft_email"],
        },
    ]
    # A category this mode does not offer has no settings section at all.
    return [c for c in categories if is_category_available(c["id"])]
