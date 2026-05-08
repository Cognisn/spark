"""MCP Server management endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spark.mcp_integration.manager import MCPClient, MCPManager, MCPServerConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/mcp")


@router.get("/api/list")
async def list_servers(request: Request) -> JSONResponse:
    """List all configured MCP servers."""
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


@router.post("/api/save")
async def save_server(request: Request) -> JSONResponse:
    """Save a new or updated MCP server configuration."""
    data = await request.json()
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


@router.post("/api/test")
async def test_connection(request: Request) -> JSONResponse:
    """Test an MCP server connection without saving."""
    data = await request.json()

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
            err_msg = f"Connection refused. Ensure the server is running at the specified address."
        elif "Name or service not known" in err_msg or "getaddrinfo" in err_msg:
            err_msg = f"Could not resolve hostname. Check the URL is correct."
        elif "SSL" in err_msg or "certificate" in err_msg.lower():
            err_msg = f"SSL/TLS error: {err_msg}. Try disabling SSL verification if using self-signed certificates."
        elif "404" in err_msg:
            err_msg = f"Server returned 404 Not Found. Check the URL path is correct."
        elif "401" in err_msg or "403" in err_msg:
            err_msg = f"Authentication failed ({err_msg}). Check your credentials."

        return JSONResponse({"status": "error", "message": err_msg}, status_code=400)


@router.get("/api/{server_name}/config")
async def get_server_config(request: Request, server_name: str) -> JSONResponse:
    """Get the full configuration for a specific MCP server (for editing)."""
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


# -- Helpers ------------------------------------------------------------------


def _load_server_configs(request: Request) -> list[dict]:
    """Load MCP server configs from config.yaml."""
    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    if not config_path.exists():
        return []
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
        return raw.get("mcp", {}).get("servers", [])
    except Exception:
        return []


def _save_server_to_config(request: Request, srv_config: dict) -> None:
    """Save/update a server in config.yaml."""
    from spark.core.application import _get_config_path

    config_path = _get_config_path()
    raw = {}
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


async def _connect_server(request: Request, srv_config: dict) -> None:
    """Connect an MCP server and add it to the manager."""
    mcp_mgr = getattr(request.app.state, "mcp_manager", None)
    if mcp_mgr is None:
        mcp_mgr = MCPManager()
        request.app.state.mcp_manager = mcp_mgr

    config = MCPServerConfig(
        name=srv_config["name"],
        transport=srv_config.get("transport", "stdio"),
        enabled=srv_config.get("enabled", True),
        command=srv_config.get("command"),
        args=srv_config.get("args", []),
        env=srv_config.get("env", {}),
        url=srv_config.get("url"),
        auth_type=srv_config.get("auth_type", "none"),
        auth_token=srv_config.get("auth_token"),
        auth_header_name=srv_config.get("auth_header_name", "X-API-Key"),
        basic_username=srv_config.get("basic_username"),
        basic_password=srv_config.get("basic_password"),
        custom_headers=srv_config.get("custom_headers", {}),
        timeout=float(srv_config.get("timeout", 30)),
        ssl_verify=srv_config.get("ssl_verify", True),
    )

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
