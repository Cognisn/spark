"""MCP server management endpoints, for whoever administers the instance.

Personal mode reads and writes ``config.yaml``'s ``mcp`` section, as before,
with every transport, and connects a saved server at once.

Server mode (A12) keeps the servers in the ``mcp.servers`` instance setting,
never ``config.yaml``: a stdio server is refused with a clear message, each
server lists the roles it is granted to, and its credentials go to konfig's
secrets store (A9), write-only. A change is saved to the setting, which
reconnects the affected servers on every replica through the settings follower.
Only a holder of ``spark.settings.instance`` may use these endpoints.

Every connection, a test one included, is made through conduit's MCP client on
the process's conduit loop (:class:`~spark.mcp_integration.manager.MCPManager`
carries the work there from the request's loop).

OAuth servers (:mod:`spark.mcp_integration.oauth`): the client secret is a
credential like the others, in the secrets store in both modes, write-only. The
administrator signs in from the settings page (``.../oauth/sign-in``, which sets
the binding cookie and answers the authorisation URL), the authorisation server
returns the browser to :data:`~spark.mcp_integration.oauth.CALLBACK_PATH`
(:data:`callback_router`, public: the return is cross-site and carries no session
cookie), and the page there says how it ended. A changed URL, client id or issuer,
a sign-out and the server's removal drop the tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as _secrets
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spark.mcp_integration import oauth
from spark.mcp_integration import servers as mcp_servers
from spark.mcp_integration.manager import (
    MCPClient,
    MCPManager,
    MCPServerConfig,
    RefreshResult,
    run_on,
)
from spark.mcp_integration.servers import STDIO_REFUSED, ServerEntryError

logger = logging.getLogger(__name__)


def _server() -> bool:
    from spark.web.scope import is_server

    return is_server()


def _instance_settings(request: Request) -> None:
    """On a server, refuse anyone without spark.settings.instance (personal mode: no check)."""
    from spark.identity.access import current_identity
    from spark.identity.permissions import SETTINGS_INSTANCE

    if not _server():
        return
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="not signed in")
    if SETTINGS_INSTANCE not in identity.permissions:
        raise HTTPException(status_code=403, detail="forbidden")


router = APIRouter(prefix="/settings/mcp", dependencies=[Depends(_instance_settings)])
#: The OAuth callback alone: reached without a session (see the module), bound by
#: the sign-in's own cookie.
callback_router = APIRouter()


# -- Server mode: the instance setting and the secrets store ----------------------------


async def _source(request: Request) -> Any:
    from spark import instance_settings
    from spark.web.db import database

    return await instance_settings.server_settings(
        request.app, database(request), request.app.state.ctx.settings
    )


def _store(request: Request) -> Any:
    return request.app.state.ctx.secrets


def _lookup(request: Request) -> mcp_servers.SecretLookup:
    store = _store(request)

    def get(name: str) -> str | None:
        value = store.get(name)
        return str(value) if value else None

    return get


def _actor(request: Request) -> str:
    from spark.instance_settings import access

    return access.access_check().user_guid(request) or "operator"


async def _entries(request: Request) -> list[dict[str, Any]]:
    value = (await _source(request)).get("mcp.servers")
    return [dict(e) for e in value] if isinstance(value, list) else []


async def _store_entries(request: Request, entries: list[dict[str, Any]]) -> None:
    source = await _source(request)
    await source.set("mcp.servers", entries, actor=_actor(request))


def _record_secret(request: Request, name: str, field: str, action: str) -> None:
    from spark.instance_settings import audit
    from spark.instance_settings.audit import SettingChange

    audit.record(
        SettingChange(
            f"mcp.servers.{name}.{field}",
            "instance",
            _actor(request),
            "set" if action == "set" else "clear",
            True,
        )
    )


def _set_secrets(request: Request, name: str, given: dict[str, str], cleared: list[str]) -> bool:
    """Write the credentials given and remove those cleared; True when anything changed."""
    store = _store(request)
    changed = False
    for field, value in given.items():
        store.set(mcp_servers.secret_name(name, field), value)
        _record_secret(request, name, field, "set")
        changed = True
    for field in cleared:
        if field in mcp_servers.SECRET_FIELDS and field not in given:
            secret = mcp_servers.secret_name(name, field)
            if store.get(secret):
                store.delete(secret)
                _record_secret(request, name, field, "clear")
                changed = True
    return changed


def _secrets_changed(request: Request) -> None:
    """A secret changes no row: refresh this replica's consumers directly."""
    follower = getattr(request.app.state, "settings_follower", None)
    if follower is not None:
        follower.changed()


def _refused(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _oauth_fields(data: dict[str, Any]) -> dict[str, Any]:
    """The OAuth fields a request gives (checked later, with the entry)."""
    scopes = data.get("oauth_scopes", [])
    return {
        "oauth_client_id": str(data.get("oauth_client_id") or "").strip() or None,
        "oauth_issuer": str(data.get("oauth_issuer") or "").strip() or None,
        "oauth_scopes": scopes if isinstance(scopes, list) else str(scopes or "").split(),
    }


def _server_entry(
    data: dict[str, Any], stored: mcp_servers.SecretLookup | None = None
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """The entry the request describes (validated), its credentials and those to clear.

    ``stored`` reads the secrets store, to tell whether an OAuth client secret is
    kept there when the request gives none.
    """
    transport = data.get("transport", "http")
    entry: dict[str, Any] = {
        "name": str(data.get("name", "")).strip(),
        "transport": transport,
        "enabled": bool(data.get("enabled", True)),
        "url": str(data.get("url", "")).strip(),
        "timeout": float(data.get("timeout", 30)),
        "ssl_verify": bool(data.get("ssl_verify", True)),
        "auth_type": data.get("auth_type", "none"),
        "roles": data.get("roles", []),
    }
    if entry["auth_type"] == "api_key":
        entry["auth_header_name"] = data.get("auth_header_name", "X-API-Key")
    if entry["auth_type"] == "basic":
        entry["basic_username"] = data.get("basic_username", "")
    if entry["auth_type"] == "oauth":
        entry.update(_oauth_fields(data))
    headers = data.get("custom_headers", {})
    if isinstance(headers, str):
        try:
            headers = json.loads(headers) if headers.strip() else {}
        except json.JSONDecodeError:
            raise ServerEntryError("custom headers must be a JSON object") from None
    _, given = mcp_servers.split_secrets(
        {
            "auth_token": data.get("auth_token", ""),
            "basic_password": data.get("basic_password", ""),
            "custom_headers": headers if isinstance(headers, dict) else {},
            "oauth_client_secret": (
                data.get("oauth_client_secret", "") if entry["auth_type"] == "oauth" else ""
            ),
        }
    )
    raw_cleared = data.get("clear_secrets", [])
    cleared = [str(c) for c in raw_cleared] if isinstance(raw_cleared, list) else []
    checked = mcp_servers.validate_entry(entry, server=True)
    if checked.get("auth_type") == "oauth":
        mcp_servers.check_oauth(
            checked, secret_set=_secret_kept(checked["name"], given, cleared, stored)
        )
    return checked, given, cleared


def _secret_kept(
    name: str,
    given: dict[str, str],
    cleared: list[str],
    stored: mcp_servers.SecretLookup | None,
) -> bool:
    """Whether the server will have an OAuth client secret once this request applies."""
    if "oauth_client_secret" in given:
        return True
    if "oauth_client_secret" in cleared or stored is None:
        return False
    return bool(stored(mcp_servers.secret_name(name, "oauth_client_secret")))


def _status(request: Request, entry: dict[str, Any]) -> dict[str, Any]:
    """An entry for the editor: no credential, with its connection and tool count."""
    shown = mcp_servers.public_entry(entry, _lookup(request))
    manager = getattr(request.app.state, "mcp_manager", None)
    name = entry.get("name")
    client = manager.servers.get(name) if manager is not None else None
    shown["connected"] = bool(client is not None and client.connected)
    shown["tool_count"] = _admin_tool_count(manager, name)
    _add_oauth_status(request, entry, shown)
    return shown


def _add_oauth_status(request: Request, entry: dict[str, Any], shown: dict[str, Any]) -> None:
    """An OAuth server's sign-in status, for the editor (never a token)."""
    if entry.get("auth_type") != "oauth":
        return
    store = _token_store(request, str(entry.get("name", "")))
    shown["oauth"] = (
        oauth.oauth_status(store)
        if store is not None
        else {"state": "failed", "reason": "store", "message": oauth.reason_text("store")}
    )


def _token_store(request: Request, name: str) -> oauth.SecretsTokenStore | None:
    manager = getattr(request.app.state, "mcp_manager", None)
    if isinstance(manager, MCPManager) and manager.token_secrets is not None:
        return manager.token_store(name)
    store = getattr(getattr(request.app.state, "ctx", None), "secrets", None)
    return oauth.token_store(store, name) if store is not None else None


#: The fields whose change means the tokens were issued for something else.
_OAUTH_IDENTITY = ("url", "transport", "auth_type", "oauth_client_id", "oauth_issuer")


def _oauth_identity_changed(before: dict[str, Any] | None, after: dict[str, Any]) -> bool:
    if before is None or before.get("auth_type") != "oauth":
        return False
    return any(before.get(f) != after.get(f) for f in _OAUTH_IDENTITY)


async def _forget_tokens(request: Request, name: str) -> None:
    """Drop ``name``'s OAuth tokens and registration (they belong to what it was)."""
    store = _token_store(request, name)
    if store is not None:
        await asyncio.to_thread(store.forget)


def _admin_tool_count(manager: Any, name: Any) -> int:
    """The tools a server offers, for its administrator (not a grant to use them)."""
    if manager is None:
        return 0
    return sum(1 for t in (manager._tools_cache or []) if t.get("server") == name)


# -- Endpoints ------------------------------------------------------------------------------


@router.get("/api/list")
async def list_servers(request: Request) -> JSONResponse:
    """List all configured MCP servers."""
    if _server():
        return JSONResponse([_status(request, e) for e in await _entries(request)])
    servers = _load_server_configs(request)
    result = []
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    for srv in servers:
        connected = False
        tool_count = 0
        enabled = srv.get("enabled", True)
        if enabled and mcp_mgr and srv["name"] in mcp_mgr.servers:
            client = mcp_mgr.servers[srv["name"]]
            connected = client.connected
            if mcp_mgr._tools_cache:
                tool_count = sum(1 for t in mcp_mgr._tools_cache if t.get("server") == srv["name"])
        shown = _personal_public(request, srv)
        result.append(
            {**shown, "enabled": enabled, "connected": connected, "tool_count": tool_count}
        )
    return JSONResponse(result)


@router.get("/api/roles")
async def list_roles(request: Request) -> JSONResponse:
    """The roles a server can be granted to (server mode's editor)."""
    from spark.identity.permissions import ROLES

    return JSONResponse(sorted(ROLES))


@router.post("/api/save")
async def save_server(request: Request) -> JSONResponse:
    """Save a new or updated MCP server configuration."""
    data = await request.json()
    if _server():
        return await _save_server_mode(request, data)
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)

    transport = data.get("transport", "stdio")
    if transport not in ("stdio", "http", "sse"):
        return JSONResponse({"error": "Invalid transport"}, status_code=400)

    # Build server config dict
    srv_config = {
        "name": name,
        "transport": transport,
        "enabled": data.get("enabled", True),
        "timeout": float(data.get("timeout", 30)),
        "ssl_verify": data.get("ssl_verify", True),
    }

    if transport == "stdio":
        srv_config["command"] = data.get("command", "")
        srv_config["args"] = data.get("args", [])
        env = data.get("env", {})
        if isinstance(env, str):
            try:
                env = json.loads(env)
            except json.JSONDecodeError:
                env = {}
        srv_config["env"] = env
    else:
        srv_config["url"] = data.get("url", "")

    # Auth
    srv_config["auth_type"] = data.get("auth_type", "none")
    if srv_config["auth_type"] == "bearer":
        srv_config["auth_token"] = data.get("auth_token", "")
    elif srv_config["auth_type"] == "api_key":
        srv_config["auth_token"] = data.get("auth_token", "")
        srv_config["auth_header_name"] = data.get("auth_header_name", "X-API-Key")
    elif srv_config["auth_type"] == "basic":
        srv_config["basic_username"] = data.get("basic_username", "")
        srv_config["basic_password"] = data.get("basic_password", "")
    elif srv_config["auth_type"] == "oauth":
        srv_config.update(_oauth_fields(data))

    previous = next((s for s in _load_server_configs(request) if s.get("name") == name), None)
    write_secret: str | None = None
    if srv_config["auth_type"] == "oauth":
        try:
            write_secret = _personal_oauth(request, srv_config, data, previous)
        except ServerEntryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # Custom headers
    custom_headers = data.get("custom_headers", {})
    if isinstance(custom_headers, str):
        try:
            custom_headers = json.loads(custom_headers)
        except json.JSONDecodeError:
            custom_headers = {}
    if custom_headers:
        srv_config["custom_headers"] = custom_headers

    if _oauth_identity_changed(previous, srv_config):
        await _forget_tokens(request, name)

    # Save to config.yaml (an OAuth client secret goes to the secrets store first)
    try:
        _apply_personal_secret(request, name, write_secret)
        _save_server_to_config(request, srv_config)
    except Exception as e:
        return JSONResponse({"error": f"Failed to save: {e}"}, status_code=500)

    # Hot-reload: connect the server
    try:
        await _connect_server(request, srv_config)
    except Exception as e:
        logger.warning("Server saved but failed to connect: %s", e)

    return JSONResponse({"status": "ok", "name": name})


async def _save_server_mode(request: Request, data: dict[str, Any]) -> JSONResponse:
    from spark.instance_settings.registry import SettingsError

    if data.get("transport") == "stdio":
        return _refused(STDIO_REFUSED)
    try:
        entry, given, cleared = _server_entry(data, _lookup(request))
    except ServerEntryError as exc:
        return _refused(str(exc))
    entries = await _entries(request)
    names = [e.get("name") for e in entries]
    before = entries[names.index(entry["name"])] if entry["name"] in names else None
    identity_changed = _oauth_identity_changed(before, entry)
    if (
        before is not None
        and before.get("oauth_session")
        and entry.get("auth_type") == "oauth"
        and not identity_changed
    ):
        entry["oauth_session"] = before["oauth_session"]
    if before is not None:
        entries[names.index(entry["name"])] = entry
    else:
        entries.append(entry)
    if identity_changed:
        await _forget_tokens(request, entry["name"])
    try:
        await _store_entries(request, entries)
    except SettingsError as exc:
        return _refused(str(exc))
    if _set_secrets(request, entry["name"], given, cleared):
        _secrets_changed(request)
    return JSONResponse({"status": "ok", "name": entry["name"]})


@router.post("/api/test")
async def test_connection(request: Request) -> JSONResponse:
    """Test an MCP server connection without saving."""
    data = await request.json()

    if _server():
        if data.get("transport", "http") == "stdio":
            return JSONResponse({"status": "error", "message": STDIO_REFUSED}, status_code=400)
        stored = _lookup(request)
        try:
            entry, given, _ = _server_entry({**data, "enabled": True}, stored)
        except ServerEntryError as exc:
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)
        name = entry["name"]
        if entry.get("auth_type") == "oauth":
            saved = next((e for e in await _entries(request) if e.get("name") == name), None)
            if saved is None or _oauth_identity_changed(saved, entry):
                return _save_first()

        def lookup(secret: str) -> str | None:
            field = secret[len(f"mcp_server_{name}_") :]
            return given.get(field) or stored(secret)

        config = mcp_servers.to_config(entry, lookup)
        config.timeout = float(data.get("timeout", 10))
    else:
        config = MCPServerConfig(
            name=data.get("name", "test"),
            transport=data.get("transport", "stdio"),
            enabled=True,
            command=data.get("command"),
            args=data.get("args", []),
            env=data.get("env", {}),
            url=data.get("url"),
            auth_type=data.get("auth_type", "none"),
            auth_token=data.get("auth_token"),
            auth_header_name=data.get("auth_header_name", "X-API-Key"),
            basic_username=data.get("basic_username"),
            basic_password=data.get("basic_password"),
            custom_headers=data.get("custom_headers", {}),
            timeout=float(data.get("timeout", 10)),
            ssl_verify=data.get("ssl_verify", True),
        )
        if config.auth_type == "oauth":
            saved = next(
                (s for s in _load_server_configs(request) if s.get("name") == config.name), None
            )
            candidate = {"auth_type": "oauth", **_oauth_fields(data)}
            candidate.update(url=config.url, transport=config.transport)
            if saved is None or _oauth_identity_changed(saved, candidate):
                return _save_first()
            config = mcp_servers.to_config(saved, None)
            config.timeout = float(data.get("timeout", 10))

    failure: list[str] = []
    tools = await _probe(request, config, failure)
    if tools is not None:
        return JSONResponse(
            {
                "status": "ok",
                "message": f"Connected successfully. {len(tools)} tools available.",
                "tools": [t["name"] for t in tools],
            }
        )
    transport = data.get("transport", "stdio")
    if config.auth_type == "oauth" and failure and failure[0] in oauth.REASONS:
        return JSONResponse(
            {"status": "error", "message": oauth.reason_text(failure[0]), "reason": failure[0]},
            status_code=400,
        )
    return JSONResponse(
        {
            "status": "error",
            "message": (
                f"Connection to '{data.get('name', '')}' failed via {transport}. Check the "
                "server is running, the URL (https, or http on this machine only) or command "
                "is correct, and the credentials are right."
            ),
        },
        status_code=400,
    )


def _save_first() -> JSONResponse:
    return JSONResponse(
        {
            "status": "error",
            "message": "Save this OAuth server and sign in to it, then test the connection.",
        },
        status_code=400,
    )


async def _probe(
    request: Request, config: MCPServerConfig, failure: list[str] | None = None
) -> list[dict[str, Any]] | None:
    """Connect ``config`` once, without registering it: its tools, or ``None``.

    ``failure`` receives conduit's failure class when the connection fails.
    """
    manager = getattr(request.app.state, "mcp_manager", None)
    if isinstance(manager, MCPManager):
        made = manager.new_client(config)
        tools = await manager.probe(made)
        reason = getattr(made, "last_failure", None)
        if tools is None and failure is not None and reason:
            failure.append(str(reason))
        return tools
    client = MCPClient(
        config,
        secrets=getattr(request.app.state, "mcp_secrets", None),
        tokens=_token_store(request, config.name),
    )
    loop = getattr(getattr(request.app.state, "mcp_runtime", None), "loop", None)

    async def probe() -> list[dict[str, Any]] | None:
        if not await client.connect():
            if failure is not None and client.last_failure:
                failure.append(client.last_failure)
            return None
        try:
            return await client.list_tools()
        finally:
            await client.disconnect()

    return await run_on(loop, probe())


# -- OAuth sign-in ----------------------------------------------------------------------------


async def _server_config(request: Request, name: str) -> MCPServerConfig | None:
    """The saved configuration of server ``name``, credentials from the store."""
    if _server():
        for entry in await _entries(request):
            if entry.get("name") == name:
                return mcp_servers.to_config(entry, _lookup(request))
        return None
    for srv in _load_server_configs(request):
        if srv.get("name") == name:
            return mcp_servers.to_config(srv, None)
    return None


def _public_base(request: Request) -> str:
    """The instance's own address: server mode's public base URL, or the address the
    administrator's browser uses (personal mode, on this machine)."""
    if _server():
        from spark.instance_settings.startup import public_base_url

        return public_base_url(request.app.state.ctx.settings.get("server.public_base_url", ""))
    return str(request.base_url).rstrip("/")


def _secure_cookie(request: Request) -> bool:
    return _server() or request.url.scheme == "https"


def _requester(request: Request) -> dict[str, Any]:
    """Who started a sign-in, for its audit events (ids only)."""
    from spark.identity.access import current_identity

    identity = current_identity(request)
    if identity is None:
        return {}
    who: dict[str, Any] = {"user_guid": identity.user_guid or None}
    if identity.warden_user_id:
        who["actor"] = identity.warden_user_id
    return who


def _ensure_manager(request: Request) -> MCPManager | None:
    manager = getattr(request.app.state, "mcp_manager", None)
    if isinstance(manager, MCPManager):
        return manager
    if _server():
        return None  # start-up always binds one in server mode
    _bind_manager(request.app.state)
    manager = request.app.state.mcp_manager
    return manager if isinstance(manager, MCPManager) else None


async def _signed_in_changed(request: Request, name: str, actor: dict[str, Any]) -> None:
    """Reconnect ``name`` with its new tokens (or none): on every replica in server mode
    (a new ``oauth_session`` in the setting), here in personal mode."""
    if _server():
        entries = await _entries(request)
        for entry in entries:
            if entry.get("name") == name:
                entry["oauth_session"] = _secrets.token_hex(8)
                source = await _source(request)
                await source.set("mcp.servers", entries, actor=actor.get("actor") or "operator")
                return
        return
    manager = getattr(request.app.state, "mcp_manager", None)
    config = await _server_config(request, name)
    if isinstance(manager, MCPManager) and config is not None and config.enabled:
        try:
            await manager.connect_server(config)
        except Exception as exc:  # noqa: BLE001 - the status shows what happened
            logger.warning("MCP server '%s' did not reconnect: %s", name, type(exc).__name__)


def _oauth_refused(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"status": "failed", "message": message}, status_code=status)


@router.post("/api/{server_name}/oauth/sign-in")
async def oauth_sign_in(request: Request, server_name: str) -> JSONResponse:
    """Start the OAuth sign-in of a saved server: the authorisation URL to open.

    Sets the binding cookie the callback needs. A server already signed in answers
    ``signed_in``; one conduit refuses answers ``failed`` with conduit's reason.
    """
    config = await _server_config(request, server_name)
    if config is None:
        return _oauth_refused("Server not found", 404)
    if config.auth_type != "oauth" or config.transport not in mcp_servers.SERVER_TRANSPORTS:
        return _oauth_refused("This server does not use OAuth.")
    manager = _ensure_manager(request)
    store = manager.token_store(server_name) if manager is not None else None
    if manager is None or store is None:
        return _oauth_refused(oauth.reason_text("store"))
    client = manager.new_client(config)
    make_client = getattr(client, "oauth_client", None)
    if make_client is None:
        return _oauth_refused("This server does not use OAuth.")
    redirect_uri = _public_base(request) + oauth.CALLBACK_PATH
    actor = _requester(request)
    started = await run_on(
        manager.loop,
        manager.sign_ins.start(server_name, make_client, redirect_uri, store=store, actor=actor),
    )
    if started.state == "redirect":
        response = JSONResponse(
            {"status": "redirect", "authorization_url": started.authorization_url}
        )
        response.set_cookie(
            oauth.BINDING_COOKIE,
            str(started.nonce),
            max_age=oauth.SIGN_IN_SECONDS,
            path=oauth.CALLBACK_PATH,
            secure=_secure_cookie(request),
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    if started.state == "signed_in":
        return JSONResponse({"status": "signed_in"})
    from spark.identity import audit

    await audit.emit_request(
        request,
        audit.MCP_OAUTH_SIGNED_IN,
        "failure",
        resource_type="mcp_server",
        resource_id=server_name,
        details={"server": server_name, "reason": started.reason},
    )
    return JSONResponse(
        {
            "status": "failed",
            "reason": started.reason,
            "message": oauth.reason_text(started.reason),
        },
        status_code=400,
    )


@router.post("/api/{server_name}/oauth/sign-out")
async def oauth_sign_out(request: Request, server_name: str) -> JSONResponse:
    """Drop a server's OAuth tokens (its registration is kept, as conduit keeps it)."""
    config = await _server_config(request, server_name)
    if config is None:
        return _oauth_refused("Server not found", 404)
    if config.auth_type != "oauth":
        return _oauth_refused("This server does not use OAuth.")
    manager = _ensure_manager(request)
    store = manager.token_store(server_name) if manager is not None else None
    if manager is None or store is None:
        return _oauth_refused(oauth.reason_text("store"))
    client = manager.new_client(config)
    signed_out = getattr(client, "oauth_client", None)
    if signed_out is None:
        return _oauth_refused("This server does not use OAuth.")
    await run_on(manager.loop, signed_out(None).sign_out())
    store.last_failure = None
    from spark.identity import audit

    await audit.emit_request(
        request,
        audit.MCP_OAUTH_SIGNED_OUT,
        resource_type="mcp_server",
        resource_id=server_name,
        details={"server": server_name},
    )
    await _signed_in_changed(request, server_name, _requester(request))
    return JSONResponse({"status": "ok"})


def _received_url(request: Request) -> str:
    """The callback's full address under the instance's own address, as received."""
    query = request.url.query
    return _public_base(request) + request.url.path + (f"?{query}" if query else "")


@callback_router.get(oauth.CALLBACK_PATH, response_class=HTMLResponse)
async def oauth_callback(request: Request) -> HTMLResponse:
    """Where the authorisation server returns the browser: complete the sign-in the
    binding cookie names, and say how it ended."""
    manager = getattr(request.app.state, "mcp_manager", None)
    nonce = request.cookies.get(oauth.BINDING_COOKIE)
    if isinstance(manager, MCPManager):
        outcome = await run_on(
            manager.loop, manager.sign_ins.complete(nonce, _received_url(request))
        )
    else:
        outcome = oauth.Outcome(False, reason="unknown_sign_in")
    if outcome.server is not None:
        from spark.identity import audit

        await audit.emit(
            audit.MCP_OAUTH_SIGNED_IN,
            "success" if outcome.signed_in else "failure",
            resource_type="mcp_server",
            resource_id=outcome.server,
            details={"server": outcome.server, "reason": outcome.reason},
            **outcome.actor,
        )
    if outcome.signed_in and outcome.server is not None:
        await _signed_in_changed(request, outcome.server, outcome.actor)
    response: HTMLResponse = request.app.state.templates.TemplateResponse(
        request,
        "mcp_sign_in.html",
        {
            "signed_in": outcome.signed_in,
            "server": outcome.server,
            "message": oauth.reason_text(outcome.reason),
        },
        status_code=200 if outcome.signed_in else 400,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.delete_cookie(
        oauth.BINDING_COOKIE,
        path=oauth.CALLBACK_PATH,
        secure=_secure_cookie(request),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/api/{server_name}/config")
async def get_server_config(request: Request, server_name: str) -> JSONResponse:
    """Get the full configuration for a specific MCP server (for editing)."""
    if _server():
        for entry in await _entries(request):
            if entry.get("name") == server_name:
                shown = mcp_servers.public_entry(entry, _lookup(request))
                _add_oauth_status(request, entry, shown)
                return JSONResponse(shown)
        return JSONResponse({"error": "Server not found"}, status_code=404)
    servers = _load_server_configs(request)
    for srv in servers:
        if srv.get("name") == server_name:
            return JSONResponse(_personal_public(request, srv))
    return JSONResponse({"error": "Server not found"}, status_code=404)


@router.get("/api/{server_name}/tools")
async def get_server_tools(request: Request, server_name: str) -> JSONResponse:
    """Get the list of tools exposed by a specific MCP server."""
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if not mcp_mgr:
        return JSONResponse({"name": server_name, "tools": []})

    tools_cache = mcp_mgr._tools_cache or []
    server_tools = [
        {"name": t.get("name", ""), "description": t.get("description", "")}
        for t in tools_cache
        if t.get("server") == server_name
    ]

    return JSONResponse({"name": server_name, "tools": server_tools})


@router.post("/api/{server_name}/tools/refresh")
async def refresh_server_tools(request: Request, server_name: str) -> JSONResponse:
    """Re-list one server's tools, reconnecting it first if it dropped.

    Answers the new list and what changed by name (``added``, ``removed``,
    ``changed``). A server that fails or times out keeps its old list and answers 502
    with the error. Guarded as the other management routes are; audited in server
    mode (the server and counts only). Only this process's manager is refreshed.
    """
    manager = getattr(request.app.state, "mcp_manager", None)
    if not isinstance(manager, MCPManager) or server_name not in manager.servers:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    result = await manager.refresh_server(server_name)
    failed = result.error is not None
    await _audit_refresh(request, server_name, result)
    body: dict[str, Any] = {
        "name": server_name,
        "status": "failed" if failed else "ok",
        "tools": [
            {"name": t.get("name", ""), "description": t.get("description", "")}
            for t in result.tools
        ],
        "added": result.added,
        "removed": result.removed,
        "changed": result.changed,
    }
    if failed:
        body["error"] = result.error
    return JSONResponse(body, status_code=502 if failed else 200)


async def _audit_refresh(request: Request, server_name: str, result: RefreshResult) -> None:
    from spark.identity import audit

    await audit.emit_request(
        request,
        audit.MCP_TOOLS_REFRESHED,
        "failure" if result.error is not None else "success",
        resource_type="mcp_server",
        resource_id=server_name,
        details={
            "server": server_name,
            "added": len(result.added),
            "removed": len(result.removed),
            "changed": len(result.changed),
        },
    )


@router.post("/api/toggle")
async def toggle_server(request: Request) -> JSONResponse:
    """Enable or disable an MCP server with immediate effect."""
    data = await request.json()
    name = data.get("name", "")
    enabled = data.get("enabled", True)

    if _server():
        entries = await _entries(request)
        for entry in entries:
            if entry.get("name") == name:
                entry["enabled"] = bool(enabled)
                await _store_entries(request, entries)
                return JSONResponse({"status": "ok"})
        return JSONResponse({"error": "Server not found"}, status_code=404)

    # Update config.yaml
    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text()) or {}
        for srv in raw.get("mcp", {}).get("servers", []):
            if srv.get("name") == name:
                srv["enabled"] = enabled
                break
        config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))

    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr and name in mcp_mgr.servers:
        try:
            await mcp_mgr.set_enabled(name, bool(enabled))
        except Exception as e:
            logger.warning("Failed to %s '%s': %s", "connect" if enabled else "disconnect", name, e)

    return JSONResponse({"status": "ok"})


@router.delete("/api/{server_name}")
async def delete_server(request: Request, server_name: str) -> JSONResponse:
    """Remove an MCP server configuration."""
    if _server():
        entries = await _entries(request)
        kept = [e for e in entries if e.get("name") != server_name]
        if len(kept) == len(entries):
            return JSONResponse({"error": "Server not found"}, status_code=404)
        await _store_entries(request, kept)
        if _set_secrets(request, server_name, {}, list(mcp_servers.SECRET_FIELDS)):
            _secrets_changed(request)
        await _forget_tokens(request, server_name)
        return JSONResponse({"status": "ok"})

    try:
        _remove_server_from_config(request, server_name)
        _apply_personal_secret(request, server_name, "")
    except Exception as e:
        return JSONResponse({"error": f"Failed to remove: {e}"}, status_code=500)
    await _forget_tokens(request, server_name)

    # Disconnect if connected
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr and server_name in mcp_mgr.servers:
        try:
            await mcp_mgr.remove_server(server_name)
        except Exception as e:
            logger.warning("Failed to disconnect '%s': %s", server_name, e)

    return JSONResponse({"status": "ok"})


# -- Helpers (personal mode: config.yaml) -----------------------------------------------


def _personal_secret_name(name: str) -> str:
    """The secrets-store name of a personal server's OAuth client secret.

    Personal names are free text; a ``secret://`` reference takes letters, digits and
    ``_.-`` only, so any other name is made safe and given a digest of the original.
    """
    import hashlib
    import re

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if safe != name:
        safe = f"{safe}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"
    return mcp_servers.secret_name(safe, "oauth_client_secret")


def _personal_oauth(
    request: Request,
    srv_config: dict[str, Any],
    data: dict[str, Any],
    previous: dict[str, Any] | None,
) -> str | None:
    """Check a personal OAuth server and point it at its client secret.

    Returns the secret to write (``""`` to remove the stored one, ``None`` to leave
    it). The entry holds only a ``secret://`` reference, never the secret.
    """
    given = str(data.get("oauth_client_secret") or "")
    raw_cleared = data.get("clear_secrets", [])
    cleared = isinstance(raw_cleared, list) and "oauth_client_secret" in raw_cleared
    kept = bool((previous or {}).get("oauth_client_secret")) and not cleared
    srv_config.update(mcp_servers.check_oauth(srv_config, secret_set=bool(given) or kept))
    reference = f"secret://{_personal_secret_name(str(srv_config['name']))}"
    if given or kept:
        srv_config["oauth_client_secret"] = reference
    if given:
        return given
    return "" if cleared else None


def _apply_personal_secret(request: Request, name: str, value: str | None) -> None:
    if value is None:
        return
    store = _store(request)
    secret = _personal_secret_name(name)
    if value:
        store.set(secret, value)
    elif store.get(secret):
        store.delete(secret)


def _personal_public(request: Request, srv: dict[str, Any]) -> dict[str, Any]:
    """A personal entry for the page: the OAuth client secret only as set or not."""
    shown = {k: v for k, v in srv.items() if k not in ("oauth_client_secret", "oauth_session")}
    if srv.get("auth_type") == "oauth":
        shown["secrets_set"] = {"oauth_client_secret": bool(srv.get("oauth_client_secret"))}
        _add_oauth_status(request, srv, shown)
    return shown


def _load_server_configs(request: Request) -> list[dict[str, Any]]:
    """Load MCP server configs from config.yaml."""
    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    if not config_path.exists():
        return []
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
        servers = raw.get("mcp", {}).get("servers", [])
        return servers if isinstance(servers, list) else []
    except Exception:
        return []


def _save_server_to_config(request: Request, srv_config: dict[str, Any]) -> None:
    """Save/update a server in config.yaml."""
    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text()) or {}

    if "mcp" not in raw:
        raw["mcp"] = {}
    if "servers" not in raw["mcp"]:
        raw["mcp"]["servers"] = []

    # Update existing or add new
    servers = raw["mcp"]["servers"]
    found = False
    for i, s in enumerate(servers):
        if s.get("name") == srv_config["name"]:
            servers[i] = srv_config
            found = True
            break
    if not found:
        servers.append(srv_config)

    config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))


def _remove_server_from_config(request: Request, server_name: str) -> None:
    """Remove a server from config.yaml."""
    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    if not config_path.exists():
        return
    raw = yaml.safe_load(config_path.read_text()) or {}
    servers = raw.get("mcp", {}).get("servers", [])
    raw["mcp"]["servers"] = [s for s in servers if s.get("name") != server_name]
    config_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False))


def _bind_manager(state: Any) -> MCPManager:
    """No server was configured at start-up: bind a manager to conduit's loop now."""
    from spark.mcp_integration.runtime import MCPRuntime

    connections = getattr(state, "connections", None)
    ctx = getattr(state, "ctx", None)
    runtime = MCPRuntime.start(
        MCPManager(
            secrets=getattr(state, "mcp_secrets", None), tokens=getattr(ctx, "secrets", None)
        ),
        getattr(connections, "loop", None),
    )
    state.mcp_runtime = runtime
    state._mcp_loop = runtime.loop
    state.mcp_manager = runtime.manager
    conv_mgr = getattr(state, "conversation_manager", None)
    if conv_mgr:
        conv_mgr._mcp_loop = runtime.loop
    return runtime.manager


async def _connect_server(request: Request, srv_config: dict[str, Any]) -> None:
    """Connect an MCP server and add it to the manager (personal mode)."""
    state = request.app.state
    mcp_mgr = getattr(state, "mcp_manager", None)
    if mcp_mgr is None:
        mcp_mgr = _bind_manager(state)

    config = mcp_servers.to_config(srv_config, None)
    await mcp_mgr.connect_server(config)
    logger.info("MCP tool cache refreshed: %d tools total", len(mcp_mgr.cached_tools()))

    # Update conversation manager's MCP reference
    conv_mgr = getattr(state, "conversation_manager", None)
    if conv_mgr:
        conv_mgr._mcp_manager = mcp_mgr

    logger.info("MCP server '%s' connected and available", config.name)
