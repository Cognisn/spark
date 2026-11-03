"""MCP server management endpoints, for whoever administers the instance.

Personal mode reads and writes ``config.yaml``'s ``mcp`` section, as before,
with every transport, and connects a saved server at once.

Server mode (A12) keeps the servers in the ``mcp.servers`` instance setting,
never ``config.yaml``: a stdio server is refused with a clear message, each
server lists the roles it is granted to, and its credentials go to konfig's
secrets store (A9), write-only. A change is saved to the setting, which
reconnects the affected servers on every replica through the settings follower.
Only a holder of ``spark.settings.instance`` may use these endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from spark.mcp_integration import servers as mcp_servers
from spark.mcp_integration.manager import MCPClient, MCPManager, MCPServerConfig
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


def _server_entry(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """The entry the request describes (validated), its credentials and those to clear."""
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
        }
    )
    cleared = data.get("clear_secrets", [])
    checked = mcp_servers.validate_entry(entry, server=True)
    return checked, given, [str(c) for c in cleared] if isinstance(cleared, list) else []


def _status(request: Request, entry: dict[str, Any]) -> dict[str, Any]:
    """An entry for the editor: no credential, with its connection and tool count."""
    shown = mcp_servers.public_entry(entry, _lookup(request))
    manager = getattr(request.app.state, "mcp_manager", None)
    name = entry.get("name")
    client = manager.servers.get(name) if manager is not None else None
    shown["connected"] = bool(client is not None and client.connected)
    shown["tool_count"] = _admin_tool_count(manager, name)
    return shown


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
        result.append({**srv, "enabled": enabled, "connected": connected, "tool_count": tool_count})
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

    # Custom headers
    custom_headers = data.get("custom_headers", {})
    if isinstance(custom_headers, str):
        try:
            custom_headers = json.loads(custom_headers)
        except json.JSONDecodeError:
            custom_headers = {}
    if custom_headers:
        srv_config["custom_headers"] = custom_headers

    # Save to config.yaml
    try:
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
        entry, given, cleared = _server_entry(data)
    except ServerEntryError as exc:
        return _refused(str(exc))
    entries = await _entries(request)
    names = [e.get("name") for e in entries]
    if entry["name"] in names:
        entries[names.index(entry["name"])] = entry
    else:
        entries.append(entry)
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
        try:
            entry, given, _ = _server_entry({**data, "enabled": True})
        except ServerEntryError as exc:
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)
        stored = _lookup(request)
        name = entry["name"]

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

    client = MCPClient(config)
    try:
        success = await client.connect()
        if success:
            tools = await client.list_tools()
            await client.disconnect()
            return JSONResponse(
                {
                    "status": "ok",
                    "message": f"Connected successfully. {len(tools)} tools available.",
                    "tools": [t["name"] for t in tools],
                }
            )
        else:
            transport = data.get("transport", "stdio")
            return JSONResponse(
                {
                    "status": "error",
                    "message": f"Connection to '{data.get('name', '')}' failed via {transport}. Check the server is running and the configuration is correct.",
                },
                status_code=400,
            )
    except asyncio.TimeoutError:
        try:
            await client.disconnect()
        except Exception:
            pass
        return JSONResponse(
            {
                "status": "error",
                "message": f"Connection timed out after {data.get('timeout', 10)}s. The server may not be running or the URL/command is incorrect.",
            },
            status_code=400,
        )
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        err_msg = str(e)
        # Provide friendlier messages for common errors
        if "Connection refused" in err_msg:
            err_msg = "Connection refused. Ensure the server is running at the specified address."
        elif "Name or service not known" in err_msg or "getaddrinfo" in err_msg:
            err_msg = "Could not resolve hostname. Check the URL is correct."
        elif "SSL" in err_msg or "certificate" in err_msg.lower():
            err_msg = f"SSL/TLS error: {err_msg}. Try disabling SSL verification if using self-signed certificates."
        elif "404" in err_msg:
            err_msg = "Server returned 404 Not Found. Check the URL path is correct."
        elif "401" in err_msg or "403" in err_msg:
            err_msg = f"Authentication failed ({err_msg}). Check your credentials."

        return JSONResponse({"status": "error", "message": err_msg}, status_code=400)


@router.get("/api/{server_name}/config")
async def get_server_config(request: Request, server_name: str) -> JSONResponse:
    """Get the full configuration for a specific MCP server (for editing)."""
    if _server():
        for entry in await _entries(request):
            if entry.get("name") == server_name:
                return JSONResponse(mcp_servers.public_entry(entry, _lookup(request)))
        return JSONResponse({"error": "Server not found"}, status_code=404)
    servers = _load_server_configs(request)
    for srv in servers:
        if srv.get("name") == server_name:
            return JSONResponse(srv)
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
    if mcp_mgr and name in mcp_mgr._clients:
        client = mcp_mgr._clients[name]
        if enabled and not client.connected:
            # Re-connect
            try:
                await client.connect()
                mcp_mgr.invalidate_cache()
                await mcp_mgr.list_all_tools()
                logger.info("MCP server '%s' re-enabled and connected", name)
            except Exception as e:
                logger.warning("Failed to reconnect '%s': %s", name, e)
        elif not enabled and client.connected:
            # Disconnect
            try:
                await client.disconnect()
                mcp_mgr.invalidate_cache()
                logger.info("MCP server '%s' disabled and disconnected", name)
            except Exception as e:
                logger.warning("Failed to disconnect '%s': %s", name, e)

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
        return JSONResponse({"status": "ok"})

    try:
        _remove_server_from_config(request, server_name)
    except Exception as e:
        return JSONResponse({"error": f"Failed to remove: {e}"}, status_code=500)

    # Disconnect if connected
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr and server_name in mcp_mgr.servers:
        try:
            await mcp_mgr.servers[server_name].disconnect()
        except Exception:
            pass
        mcp_mgr._clients.pop(server_name, None)
        mcp_mgr.invalidate_cache()

    return JSONResponse({"status": "ok"})


# -- Helpers (personal mode: config.yaml) -----------------------------------------------


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


async def _connect_server(request: Request, srv_config: dict[str, Any]) -> None:
    """Connect an MCP server and add it to the manager."""
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr is None:
        mcp_mgr = MCPManager()
        request.app.state.mcp_manager = mcp_mgr

    config = mcp_servers.to_config(srv_config, None)

    # Remove existing client if any
    if config.name in mcp_mgr._clients:
        try:
            await mcp_mgr._clients[config.name].disconnect()
        except Exception:
            pass

    mcp_mgr.add_server(config)
    await mcp_mgr._clients[config.name].connect()
    mcp_mgr.invalidate_cache()

    # Repopulate tool cache so tool counts are available immediately
    tools = await mcp_mgr.list_all_tools()
    logger.info("MCP tool cache refreshed: %d tools total", len(tools))

    # Update conversation manager's MCP reference
    conv_mgr = getattr(request.app.state, "conversation_manager", None)
    if conv_mgr:
        conv_mgr._mcp_manager = mcp_mgr

    logger.info("MCP server '%s' connected and available", config.name)
