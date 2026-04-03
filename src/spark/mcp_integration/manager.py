"""MCP server management — connection, tool discovery, and execution."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    transport: str = "stdio"  # stdio | http | sse
    enabled: bool = True

    # Stdio transport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    # HTTP/SSE transport
    url: str | None = None

    # Authentication
    auth_type: str = "none"  # none | bearer | api_key | basic | custom
    auth_token: str | None = None
    auth_header_name: str = "X-API-Key"
    basic_username: str | None = None
    basic_password: str | None = None
    custom_headers: dict[str, str] = field(default_factory=dict)

    # Connection
    timeout: float = 30.0
    ssl_verify: bool = True


class MCPClient:
    """Client for a single MCP server."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._session: Any = None
        self._context: Any = None
        self._connected = False
        self._httpx_client: Any = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def config(self) -> MCPServerConfig:
        return self._config

    def _build_auth_headers(self) -> dict[str, str]:
        """Build authentication headers based on config."""
        headers: dict[str, str] = {}
        auth = self._config.auth_type

        if auth == "bearer" and self._config.auth_token:
            headers["Authorization"] = f"Bearer {self._config.auth_token}"
        elif auth == "api_key" and self._config.auth_token:
            headers[self._config.auth_header_name] = self._config.auth_token
        elif auth == "basic" and self._config.basic_username:
            creds = f"{self._config.basic_username}:{self._config.basic_password or ''}"
            encoded = base64.b64encode(creds.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        elif auth == "custom":
            headers.update(self._config.custom_headers)

        return headers

    async def connect(self) -> bool:
        """Connect to the MCP server. Returns True on success."""
        try:
            transport = self._config.transport
            if transport == "stdio":
                return await self._connect_stdio()
            elif transport == "http":
                return await self._connect_http()
            elif transport == "sse":
                return await self._connect_sse()
            else:
                logger.error("Unknown transport: %s", transport)
                return False
        except asyncio.TimeoutError:
            logger.error(
                "MCP server '%s' (%s) connection timed out after %.0fs",
                self._config.name,
                self._config.transport,
                self._config.timeout,
            )
            await self._cleanup()
            return False
        except Exception as e:
            logger.error(
                "MCP server '%s' (%s) connection failed: %s",
                self._config.name,
                self._config.transport,
                e,
            )
            await self._cleanup()
            return False

    async def _connect_stdio(self) -> bool:
        """Connect via stdio transport."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not self._config.command:
            raise ValueError("stdio transport requires 'command'")

        params = StdioServerParameters(
            command=self._config.command,
            args=self._config.args,
            env=self._config.env or None,
        )
        self._context = stdio_client(params)
        read, write = await self._context.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await asyncio.wait_for(self._session.initialize(), timeout=self._config.timeout)
        self._connected = True
        logger.info("Connected to MCP server %s (stdio)", self._config.name)
        return True

    def _build_all_headers(self) -> dict[str, str]:
        """Build combined auth + custom headers."""
        headers = self._build_auth_headers()
        if self._config.custom_headers:
            headers.update(self._config.custom_headers)
        return headers

    def _make_httpx_factory(self) -> Any:
        """Create an httpx client factory for SSL verification control.

        Returns None when ssl_verify is True (use default secure factory).
        When ssl_verify is False (e.g. self-signed certs), creates a factory
        that disables verification — the user explicitly opted into this.
        """
        if self._config.ssl_verify:
            return None  # Use default factory with certificate validation
        import httpx

        ssl_verify = bool(self._config.ssl_verify)  # noqa: S501 — user-configured

        def factory(
            headers: dict[str, str] | None = None,
            timeout: Any = None,
            auth: Any = None,
        ) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                auth=auth,
                verify=ssl_verify,
            )

        return factory

    async def _connect_http(self) -> bool:
        """Connect via HTTP streamable transport."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if not self._config.url:
            raise ValueError("HTTP transport requires 'url'")

        headers = self._build_all_headers()
        kwargs: dict[str, Any] = {
            "url": self._config.url,
            "timeout": self._config.timeout,
        }
        if headers:
            kwargs["headers"] = headers

        factory = self._make_httpx_factory()
        if factory:
            kwargs["httpx_client_factory"] = factory

        self._context = streamablehttp_client(**kwargs)
        read, write, _ = await self._context.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await asyncio.wait_for(self._session.initialize(), timeout=self._config.timeout)
        self._connected = True
        logger.info("Connected to MCP server %s (HTTP) at %s", self._config.name, self._config.url)
        return True

    async def _connect_sse(self) -> bool:
        """Connect via SSE transport."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        if not self._config.url:
            raise ValueError("SSE transport requires 'url'")

        headers = self._build_all_headers()
        kwargs: dict[str, Any] = {
            "url": self._config.url,
            "timeout": self._config.timeout,
        }
        if headers:
            kwargs["headers"] = headers

        factory = self._make_httpx_factory()
        if factory:
            kwargs["httpx_client_factory"] = factory

        self._context = sse_client(**kwargs)
        read, write = await self._context.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await asyncio.wait_for(self._session.initialize(), timeout=self._config.timeout)
        self._connected = True
        logger.info("Connected to MCP server %s (SSE)", self._config.name)
        return True

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        await self._cleanup()
        self._connected = False
        logger.info("Disconnected from MCP server %s", self._config.name)

    async def _cleanup(self) -> None:
        """Clean up connection resources."""
        if self._session:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None

        if self._context:
            try:
                await self._context.__aexit__(None, None, None)
            except Exception:
                pass
            self._context = None

        if self._httpx_client:
            try:
                await self._httpx_client.aclose()
            except Exception:
                pass
            self._httpx_client = None

        self._connected = False

    async def list_tools(self) -> list[dict[str, Any]]:
        """List tools available on this server."""
        if not self._session or not self._connected:
            return []

        result = await self._session.list_tools()
        tools = []
        for tool in result.tools:
            tools.append(
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "inputSchema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    "server": self._config.name,
                }
            )
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on this server."""
        if not self._session or not self._connected:
            raise RuntimeError(f"Not connected to {self._config.name}")

        result = await asyncio.wait_for(
            self._session.call_tool(tool_name, arguments),
            timeout=self._config.timeout,
        )

        content: list[dict] = []
        for item in result.content:
            if hasattr(item, "text"):
                content.append({"type": "text", "text": item.text})
            elif hasattr(item, "data") and hasattr(item, "mimeType"):
                content.append({"type": "image", "data": item.data, "mimeType": item.mimeType})
            else:
                content.append({"type": "resource", "resource": str(item)})

        return {
            "content": content,
            "isError": getattr(result, "isError", False),
        }


class MCPManager:
    """Manages multiple MCP server connections."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tools_cache: list[dict[str, Any]] | None = None

    def add_server(self, config: MCPServerConfig) -> None:
        """Add an MCP server configuration."""
        if not config.enabled:
            logger.info("Skipping disabled MCP server: %s", config.name)
            return
        self._clients[config.name] = MCPClient(config)
        self._tools_cache = None

    @property
    def servers(self) -> dict[str, MCPClient]:
        """All registered MCP clients."""
        return dict(self._clients)

    async def connect_all(self, *, progress_callback: Any | None = None) -> dict[str, bool]:
        """Connect to all registered servers. Returns name→success mapping."""
        results: dict[str, bool] = {}
        for name, client in self._clients.items():
            success = await client.connect()
            results[name] = success
            if progress_callback:
                progress_callback(name, success)
        self._tools_cache = None
        return results

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for client in self._clients.values():
            await client.disconnect()
        self._tools_cache = None

    async def list_all_tools(self) -> list[dict[str, Any]]:
        """List all tools across all connected servers (cached)."""
        if self._tools_cache is not None:
            return self._tools_cache

        tools: list[dict[str, Any]] = []
        for client in self._clients.values():
            if not client.connected:
                continue
            try:
                server_tools = await asyncio.wait_for(client.list_tools(), timeout=5.0)
                tools.extend(server_tools)
            except Exception as e:
                logger.warning("Failed to list tools from %s: %s", client.name, e)

        self._tools_cache = tools
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        server_name: str | None = None,
    ) -> dict[str, Any]:
        """Call a tool, optionally specifying the server."""
        if server_name:
            client = self._clients.get(server_name)
            if not client or not client.connected:
                raise RuntimeError(f"MCP server '{server_name}' not connected")
            return await client.call_tool(tool_name, arguments)

        # Search all servers
        all_tools = await self.list_all_tools()
        for tool in all_tools:
            if tool["name"] == tool_name:
                srv = tool["server"]
                client = self._clients.get(srv)
                if client and client.connected:
                    return await client.call_tool(tool_name, arguments)

        raise ValueError(f"Tool '{tool_name}' not found on any connected server")

    def get_server_timeout(
        self, *, server_name: str | None = None, tool_name: str | None = None
    ) -> float:
        """Get the configured timeout for a server or tool's owning server."""
        if server_name and server_name in self._clients:
            return self._clients[server_name].config.timeout

        if tool_name and self._tools_cache:
            for tool in self._tools_cache:
                if tool["name"] == tool_name:
                    srv = tool["server"]
                    if srv in self._clients:
                        return self._clients[srv].config.timeout

        return 30.0

    def invalidate_cache(self) -> None:
        """Clear the tools cache to force re-discovery."""
        self._tools_cache = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> MCPManager:
        """Create an MCPManager from a config dict.

        Expected format::

            mcp:
              servers:
                - name: my-server
                  transport: stdio
                  command: npx
                  args: ["-y", "@example/mcp-server"]
                  env:
                    API_KEY: secret://example_key
                  auth_type: bearer
                  auth_token: "..."
                  timeout: 30
                  ssl_verify: true
        """
        manager = cls()
        servers = config.get("mcp", {}).get("servers", [])

        for srv in servers:
            cfg = MCPServerConfig(
                name=srv.get("name", "unnamed"),
                transport=srv.get("transport", "stdio"),
                enabled=srv.get("enabled", True),
                command=srv.get("command"),
                args=srv.get("args", []),
                env=srv.get("env", {}),
                url=srv.get("url"),
                auth_type=srv.get("auth_type", "none"),
                auth_token=srv.get("auth_token"),
                auth_header_name=srv.get("auth_header_name", "X-API-Key"),
                basic_username=srv.get("basic_username"),
                basic_password=srv.get("basic_password"),
                custom_headers=srv.get("custom_headers", {}),
                timeout=float(srv.get("timeout", 30)),
                ssl_verify=srv.get("ssl_verify", True),
            )
            manager.add_server(cfg)

        return manager
