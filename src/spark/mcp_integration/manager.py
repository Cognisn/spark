"""MCP server management: connection, tool discovery, and execution.

Personal mode offers every connected server to its single user, as before.

Server mode (A12) offers a caller only the servers its permissions grant
(``spark.mcp.<server>``, see :mod:`spark.mcp_integration.servers`):
:meth:`MCPManager.list_all_tools` and :meth:`MCPManager.call_tool` take the
caller's ``permissions``, and without them list nothing and refuse every call
(fail closed). Every tool call, in either mode, is reported to SPARK's audit
trail (:mod:`spark.identity.audit`, a no-op in personal mode) with the user,
conversation, server, tool and outcome, including failures and refusals; the
arguments and the result are never recorded.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "MCPClient",
    "MCPManager",
    "MCPServerConfig",
    "MCPToolRefused",
    "UNSCOPED",
]


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


class MCPToolRefused(PermissionError):
    """The caller may not use this MCP server (server mode)."""


class _Unscoped:
    """No permissions were given: personal mode offers everything, a server nothing."""

    def __repr__(self) -> str:
        return "UNSCOPED"


UNSCOPED: Final = _Unscoped()

Scope = Iterable[str] | None | _Unscoped


class ClientLike(Protocol):
    """What the manager needs of a client (:class:`MCPClient`, or a test's fake)."""

    @property
    def name(self) -> str: ...

    @property
    def connected(self) -> bool: ...

    @property
    def config(self) -> MCPServerConfig: ...

    async def connect(self) -> bool: ...

    async def disconnect(self) -> None: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class MCPClient:
    """Client for a single MCP server.

    The MCP SDK's transports are anyio task groups, which must be entered and
    left in the same task. Each connection therefore lives in one task of its
    own (:meth:`_run`): it opens the transport and the session, reports that it
    is ready, waits until :meth:`disconnect` asks it to stop, and closes both in
    that same task. A transport that fails in the background (an unreachable
    server) ends the task, which :meth:`connect` reports as a failed connection
    rather than letting the cancellation escape to the caller.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._session: Any = None
        self._connected = False
        self._runner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

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

    def _build_all_headers(self) -> dict[str, str]:
        """Build combined auth + custom headers."""
        headers = self._build_auth_headers()
        if self._config.custom_headers:
            headers.update(self._config.custom_headers)
        return headers

    def _make_httpx_factory(self) -> Callable[..., Any] | None:
        """Create an httpx client factory for SSL verification control.

        Returns None when ssl_verify is True (use default secure factory).
        When ssl_verify is False (e.g. self-signed certs), creates a factory
        that disables verification — the user explicitly opted into this.
        The client is of the HTTP library the installed MCP SDK uses.
        """
        if self._config.ssl_verify:
            return None  # Use default factory with certificate validation
        httpx = _httpx_module()

        ssl_verify = bool(self._config.ssl_verify)  # noqa: S501 — user-configured

        def factory(
            headers: dict[str, str] | None = None,
            timeout: Any = None,
            auth: Any = None,
        ) -> Any:
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                auth=auth,
                verify=ssl_verify,
            )

        return factory

    async def connect(self) -> bool:
        """Connect to the MCP server. Returns True on success, False on any failure."""
        if self._connected:
            return True
        if self._config.transport not in ("stdio", "http", "sse"):
            logger.error("Unknown transport: %s", self._config.transport)
            return False
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._stop = asyncio.Event()
        runner = loop.create_task(self._run(ready, self._stop), name=f"mcp-{self.name}")
        self._runner = runner
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=self._config.timeout)
        except asyncio.TimeoutError:
            logger.error(
                "MCP server '%s' (%s) connection timed out after %.0fs",
                self._config.name,
                self._config.transport,
                self._config.timeout,
            )
            ready.cancel()
            await self._end(runner)
            return False
        except asyncio.CancelledError:
            # The caller was cancelled: close the half-open connection and pass it on.
            ready.cancel()
            await self._end(runner)
            raise
        except Exception as e:
            logger.error(
                "MCP server '%s' (%s) connection failed: %s",
                self._config.name,
                self._config.transport,
                _reason(e),
            )
            await self._end(runner)
            return False
        logger.info("Connected to MCP server %s (%s)", self._config.name, self._config.transport)
        return True

    async def _run(self, ready: asyncio.Future[None], stop: asyncio.Event) -> None:
        """Own one connection from opening to closing (see the class docstring)."""
        from mcp import ClientSession

        try:
            async with contextlib.AsyncExitStack() as stack:
                read, write = await self._open(stack)
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=self._config.timeout)
                self._session = session
                self._connected = True
                ready.set_result(None)
                await stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to connect() or logged
            if not ready.done():
                ready.set_exception(
                    RuntimeError("the transport closed")
                    if isinstance(exc, asyncio.CancelledError)
                    else exc
                )
            elif not stop.is_set() and not isinstance(exc, asyncio.CancelledError):
                logger.warning("MCP server %s connection lost: %s", self.name, _reason(exc))
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            self._session = None
            self._connected = False

    async def _open(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        """Enter the configured transport on ``stack``: its read and write streams."""
        transport = self._config.transport
        if transport == "stdio":
            return await self._open_stdio(stack)
        if not self._config.url:
            raise ValueError(f"{transport.upper()} transport requires 'url'")
        if transport == "http":
            return await self._open_http(stack, self._config.url)
        return await self._open_sse(stack, self._config.url)

    async def _open_stdio(self, stack: contextlib.AsyncExitStack) -> tuple[Any, Any]:
        """Connect via stdio transport (personal mode only)."""
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not self._config.command:
            raise ValueError("stdio transport requires 'command'")

        params = StdioServerParameters(
            command=self._config.command,
            args=self._config.args,
            env=self._config.env or None,
        )
        streams = await stack.enter_async_context(stdio_client(params))
        return streams[0], streams[1]

    async def _open_http(self, stack: contextlib.AsyncExitStack, url: str) -> tuple[Any, Any]:
        """Connect via HTTP streamable transport (MCP SDK 2.x, or 1.x)."""
        from mcp.client import streamable_http

        headers = self._build_all_headers()
        factory = self._make_httpx_factory()
        modern = getattr(streamable_http, "streamable_http_client", None)
        if modern is not None:
            # SDK 2.x: the caller builds, and closes, the HTTP client.
            timeout = _httpx_module().Timeout(self._config.timeout, read=300.0)
            make = factory or getattr(streamable_http, "create_mcp_http_client")
            client = await stack.enter_async_context(make(headers=headers or None, timeout=timeout))
            streams = await stack.enter_async_context(modern(url, http_client=client))
        else:
            kwargs: dict[str, Any] = {"url": url, "timeout": self._config.timeout}
            if headers:
                kwargs["headers"] = headers
            if factory:
                kwargs["httpx_client_factory"] = factory
            # SDK 1.x (the name differs by version, so it is looked up).
            legacy = getattr(streamable_http, "streamablehttp_client")
            streams = await stack.enter_async_context(legacy(**kwargs))
        return streams[0], streams[1]

    async def _open_sse(self, stack: contextlib.AsyncExitStack, url: str) -> tuple[Any, Any]:
        """Connect via SSE transport."""
        from mcp.client.sse import sse_client

        headers = self._build_all_headers()
        kwargs: dict[str, Any] = {"url": url, "timeout": self._config.timeout}
        if headers:
            kwargs["headers"] = headers
        factory = self._make_httpx_factory()
        if factory:
            kwargs["httpx_client_factory"] = factory
        streams = await stack.enter_async_context(sse_client(**kwargs))
        return streams[0], streams[1]

    async def _end(self, runner: asyncio.Task[None]) -> None:
        """Stop ``runner`` and wait for it, cancelling it if it does not stop in time."""
        if self._stop is not None:
            self._stop.set()
        if runner.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            runner.cancel()
        except Exception:  # noqa: BLE001 - the runner logs its own failure
            pass

    async def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        runner = self._runner
        self._runner = None
        if runner is not None:
            await self._end(runner)
        self._session = None
        self._connected = False
        logger.info("Disconnected from MCP server %s", self._config.name)

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
                    "inputSchema": _field(tool, "input_schema", "inputSchema") or {},
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

        content: list[dict[str, Any]] = []
        for item in result.content:
            mime_type = _field(item, "mime_type", "mimeType")
            if hasattr(item, "text"):
                content.append({"type": "text", "text": item.text})
            elif hasattr(item, "data") and mime_type is not None:
                content.append({"type": "image", "data": item.data, "mimeType": mime_type})
            else:
                content.append({"type": "resource", "resource": str(item)})

        return {
            "content": content,
            "isError": bool(_field(result, "is_error", "isError")),
        }


def _field(item: Any, *names: str) -> Any:
    """The first of ``names`` ``item`` has (MCP SDK 2.x snake case, then 1.x camel case)."""
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _reason(exc: BaseException) -> str:
    """A short reason for a failure: the first leaf of an exception group, as its type and text."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _httpx_module() -> Any:
    """The HTTP library the installed MCP SDK uses: ``httpx2`` (SDK 2.x) or ``httpx``."""
    try:
        import httpx2

        return httpx2
    except ImportError:
        import httpx

        return httpx


def _server_mode() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        return False


def _audit(
    refused: bool,
    result: str,
    *,
    server: str | None,
    tool: str,
    user_guid: str | None,
    conversation_id: Any,
) -> None:
    """Report one tool call (never its arguments or result); a no-op in personal mode."""
    from spark.identity import audit

    audit.submit(
        audit.MCP_TOOL_REFUSED if refused else audit.MCP_TOOL_CALLED,
        "success" if result == "success" else "failure",
        user_guid=user_guid,
        resource_type="mcp_tool",
        resource_id=f"{server}/{tool}" if server else tool,
        details={
            "server": server,
            "tool": tool,
            "conversation_id": conversation_id,
            "result": result,
        },
    )


class MCPManager:
    """Manages multiple MCP server connections."""

    def __init__(self, client_factory: Callable[[MCPServerConfig], ClientLike] = MCPClient) -> None:
        self._client_factory = client_factory
        self._clients: dict[str, ClientLike] = {}
        self._tools_cache: list[dict[str, Any]] | None = None

    def add_server(self, config: MCPServerConfig) -> None:
        """Add an MCP server configuration."""
        if not config.enabled:
            logger.info("Skipping disabled MCP server: %s", config.name)
            return
        self._clients[config.name] = self._client_factory(config)
        self._tools_cache = None

    @property
    def servers(self) -> dict[str, ClientLike]:
        """All registered MCP clients (unfiltered: for administration, never for a user)."""
        return dict(self._clients)

    # -- Scoping (server mode) -----------------------------------------------------

    def _granted(self, name: str, permissions: Scope) -> bool:
        """Whether a caller with ``permissions`` may use server ``name``."""
        if not _server_mode():
            return True
        if isinstance(permissions, _Unscoped) or permissions is None:
            return False
        from spark.mcp_integration.servers import is_granted

        return is_granted(name, permissions)

    def servers_for(self, permissions: Scope = UNSCOPED) -> dict[str, ClientLike]:
        """The registered clients a caller with ``permissions`` may see."""
        return {n: c for n, c in self._clients.items() if self._granted(n, permissions)}

    def cached_tools(self, *, permissions: Scope = UNSCOPED) -> list[dict[str, Any]]:
        """The tools already discovered, as a caller with ``permissions`` may see them."""
        return [
            t for t in (self._tools_cache or []) if self._granted(str(t.get("server")), permissions)
        ]

    # -- Connections -----------------------------------------------------------------

    async def connect_all(
        self, *, progress_callback: Callable[[str, bool], Any] | None = None
    ) -> dict[str, bool]:
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

    async def reconcile(self, configs: Iterable[MCPServerConfig]) -> set[str]:
        """Make the registered servers match ``configs``; returns the names that changed.

        A new or changed server is (re)connected, a removed or disabled one
        disconnected; unchanged servers keep their connection. The tool cache is
        rebuilt.
        """
        wanted = {c.name: c for c in configs if c.enabled}
        changed: set[str] = set()
        for name in [n for n in self._clients if n not in wanted]:
            await self._drop(name)
            changed.add(name)
        for name, config in wanted.items():
            current = self._clients.get(name)
            if current is not None and current.config == config:
                continue
            if current is not None:
                await self._drop(name)
            self.add_server(config)
            if not await self._clients[name].connect():
                logger.warning("MCP server %s did not connect after a settings change", name)
            changed.add(name)
        self._tools_cache = None
        await self.list_all_tools()
        if changed:
            logger.info("MCP servers reconnected after a settings change: %s", sorted(changed))
        return changed

    async def _drop(self, name: str) -> None:
        client = self._clients.pop(name)
        try:
            await client.disconnect()
        except Exception as e:  # noqa: BLE001 - a dead server must not stop the others
            logger.warning("Failed to disconnect MCP server %s: %s", name, e)

    # -- Tools -----------------------------------------------------------------------

    async def _discover(self) -> list[dict[str, Any]]:
        """Every tool across all connected servers (cached)."""
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

    async def list_all_tools(self, *, permissions: Scope = UNSCOPED) -> list[dict[str, Any]]:
        """The tools of the connected servers a caller with ``permissions`` may use."""
        tools = await self._discover()
        return [t for t in tools if self._granted(str(t.get("server")), permissions)]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        server_name: str | None = None,
        permissions: Scope = UNSCOPED,
        user_guid: str | None = None,
        conversation_id: Any = None,
    ) -> dict[str, Any]:
        """Call a tool, optionally on a named server, for a caller with ``permissions``.

        Raises :class:`MCPToolRefused` when the server is not granted (server
        mode), ``RuntimeError`` when it is not connected and ``ValueError`` when
        no server has the tool. Every outcome is audited.
        """

        def report(result: str, server: str | None, refused: bool = False) -> None:
            _audit(
                refused,
                result,
                server=server,
                tool=tool_name,
                user_guid=user_guid,
                conversation_id=conversation_id,
            )

        if server_name is None:
            owners = [str(t["server"]) for t in await self._discover() if t["name"] == tool_name]
            granted = [s for s in owners if self._granted(s, permissions)]
            if granted:
                server_name = granted[0]
            elif owners:
                report("refused", owners[0], refused=True)
                raise MCPToolRefused(f"MCP server '{owners[0]}' is not available to you")
            elif _server_mode() and (isinstance(permissions, _Unscoped) or permissions is None):
                report("refused", None, refused=True)
                raise MCPToolRefused(f"MCP tool '{tool_name}' is not available to you")
            else:
                report("unavailable", None)
                raise ValueError(f"Tool '{tool_name}' not found on any connected server")
        elif not self._granted(server_name, permissions):
            report("refused", server_name, refused=True)
            raise MCPToolRefused(f"MCP server '{server_name}' is not available to you")

        client = self._clients.get(server_name)
        if not client or not client.connected:
            report("unavailable", server_name)
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        try:
            result = await client.call_tool(tool_name, arguments)
        except BaseException:
            report("failed", server_name)
            raise
        report("error" if result.get("isError") else "success", server_name)
        return result

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
        """Create an MCPManager from a config dict (``config.yaml``; personal mode).

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

        A server never reads ``config.yaml``'s ``mcp`` section: in server mode the
        servers come from the instance settings, so this returns an empty manager
        and warns when the section lists servers.
        """
        from spark.mcp_integration.servers import to_config

        manager = cls()
        servers = config.get("mcp", {}).get("servers", [])
        if _server_mode():
            if servers:
                logger.warning(
                    "Ignoring config.yaml's mcp section (%d servers): server mode reads "
                    "mcp.servers from the instance settings",
                    len(servers),
                )
            return manager

        for srv in servers:
            manager.add_server(to_config(srv, None))

        return manager
