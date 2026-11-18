"""One MCP server, reached through conduit's MCP client over the official SDK (M2).

:class:`MCPClient` keeps the interface SPARK's manager has always used (connect,
list tools, call a tool, disconnect, each answering in SPARK's shapes) and leaves
the transports to :class:`conduit.mcp.McpConnection`: stdio, streamable HTTP and
SSE, one task per connection, timeouts and clean shutdown.

Settings. A :class:`MCPServerConfig` becomes conduit's ``McpServerSettings`` at
every connect, so a changed credential is read again on the next connection:

* conduit holds credentials only as ``secret://`` references (A9). A value that is
  already a reference (a personal ``config.yaml`` may say ``secret://name``) is
  resolved through SPARK's resolver over konfig, given as ``secrets``; a value
  SPARK has already read from konfig's secrets store (server mode, through
  :func:`spark.mcp_integration.servers.to_config`) or from ``config.yaml`` is held
  for this connection only, under a reference of its own.
* A bearer token and an API key are conduit's own ``BearerTokenAuth`` and
  ``ApiKeyAuth``. Basic credentials and custom headers, which conduit does not
  offer, are added to each HTTP request by a transport of SPARK's
  (:class:`_HeaderTransport`), as is ``ssl_verify: false``.
* conduit names a server ``^[a-z][a-z0-9]*(_[a-z0-9]+)*$`` (at most 32
  characters); SPARK's names are wider, so conduit is given a name derived from
  SPARK's (:func:`conduit_name`). SPARK's own name is what SPARK reports.
* stdio runs a command on the host and is refused in server mode here as well as
  where servers are configured (A12).

Results. conduit renders a tool result as text (binary content described, never
passed on). This client returns it unframed: the conversation manager frames and
inspects every MCP result as untrusted content (A16), so it is framed once, by
SPARK. conduit's tool bridge, which frames results itself, is not used.
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2
from conduit import ConduitError, SecretRef, SecretResolver
from conduit.mcp import (
    ApiKeyAuth,
    BearerTokenAuth,
    McpConnection,
    McpError,
    McpServerSettings,
    NoAuth,
    SseTransport,
    StdioTransport,
    StreamableHttpTransport,
)
from pydantic import SecretStr, ValidationError

__all__ = [
    "MCPClient",
    "MCPServerConfig",
    "StdioRefused",
    "conduit_name",
]

logger = logging.getLogger(__name__)

#: The task class every bridged server serves. SPARK decides who may use a server
#: (``spark.mcp.<server>``), so conduit's per-task grant is not used.
TASK_CLASS: Final = "spark"
#: Reference names for values held for one connection (never stored anywhere).
_HELD_PREFIX: Final = "spark.mcp.held/"
_CONDUIT_NAME_LIMIT: Final = 32


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    transport: str = "stdio"  # stdio | http | sse
    enabled: bool = True

    # Stdio transport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict, repr=False)

    # HTTP/SSE transport
    url: str | None = None

    # Authentication (credentials are never shown in a repr)
    auth_type: str = "none"  # none | bearer | api_key | basic | custom
    auth_token: str | None = field(default=None, repr=False)
    auth_header_name: str = "X-API-Key"
    basic_username: str | None = None
    basic_password: str | None = field(default=None, repr=False)
    custom_headers: dict[str, str] = field(default_factory=dict, repr=False)

    # Connection
    timeout: float = 30.0
    ssl_verify: bool = True


class StdioRefused(PermissionError):
    """A stdio server was asked for in server mode (A12)."""


def conduit_name(name: str) -> str:
    """The name conduit knows server ``name`` by: lower-case words joined by ``_``."""
    local = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not local or not local[0].isalpha():
        local = f"mcp_{local}"
    return local[:_CONDUIT_NAME_LIMIT].rstrip("_") or "mcp"


def _server_mode() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        return False


class _Secrets:
    """SPARK's resolver for one connection: held values first, then konfig (A9)."""

    def __init__(self, held: Mapping[str, str], fallback: SecretResolver | None) -> None:
        self._held = dict(held)
        self._fallback = fallback

    async def resolve(self, ref: SecretRef) -> SecretStr:
        if ref.name in self._held:
            return SecretStr(self._held[ref.name])
        if self._fallback is None:
            from conduit import SecretUnavailable

            raise SecretUnavailable(f"{ref} has no secret resolver")
        return await self._fallback.resolve(ref)

    def __repr__(self) -> str:
        return f"_Secrets(<{len(self._held)} held>, {self._fallback!r})"


class _References:
    """Turns configured credential values into references for conduit's settings."""

    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def __call__(self, key: str, value: str) -> SecretRef:
        if value.startswith("secret://"):
            return SecretRef.parse(value)
        name = _HELD_PREFIX + key
        self.held[name] = value
        return SecretRef(name)


class _HeaderTransport(httpx2.AsyncBaseTransport):
    """Adds the headers conduit's authentication does not send (basic, custom)."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, headers: Mapping[str, str]) -> None:
        self._inner = inner
        self._headers = dict(headers)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        for name, value in self._headers.items():
            request.headers[name] = value
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class MCPClient:
    """Client for a single MCP server, over :class:`conduit.mcp.McpConnection`.

    ``secrets`` is SPARK's resolver over konfig, for credentials configured as
    ``secret://`` references. :meth:`connect` reports any failure as ``False``
    (logged with SPARK's name for the server and conduit's failure class).
    """

    def __init__(self, config: MCPServerConfig, *, secrets: SecretResolver | None = None) -> None:
        self._config = config
        self._secrets = secrets
        self._connection: McpConnection | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def connected(self) -> bool:
        connection = self._connection
        return connection is not None and connection.connected

    @property
    def config(self) -> MCPServerConfig:
        return self._config

    def __repr__(self) -> str:
        return f"MCPClient({self.name!r}, connected={self.connected})"

    # -- Settings ------------------------------------------------------------------

    def _build_auth_headers(self) -> dict[str, str]:
        """Every authentication header the configuration describes, as values."""
        headers: dict[str, str] = {}
        config = self._config
        auth = config.auth_type
        if auth == "bearer" and config.auth_token:
            headers["Authorization"] = f"Bearer {config.auth_token}"
        elif auth == "api_key" and config.auth_token:
            headers[config.auth_header_name] = config.auth_token
        elif auth == "basic" and config.basic_username:
            headers.update(self._basic_header())
        elif auth == "custom":
            headers.update(config.custom_headers)
        return headers

    def _basic_header(self) -> dict[str, str]:
        config = self._config
        creds = f"{config.basic_username}:{config.basic_password or ''}"
        return {"Authorization": f"Basic {base64.b64encode(creds.encode()).decode()}"}

    def _extra_headers(self) -> dict[str, str]:
        """The headers SPARK adds itself: basic credentials and custom headers."""
        headers: dict[str, str] = {}
        if self._config.auth_type == "basic" and self._config.basic_username:
            headers.update(self._basic_header())
        headers.update(self._config.custom_headers)
        return headers

    def settings(self) -> tuple[McpServerSettings, _Secrets]:
        """conduit's settings for this server, and the resolver for their references.

        Raises :class:`StdioRefused` for stdio in server mode, ``ValueError`` for a
        configuration conduit refuses (its message names fields, never values).
        """
        config = self._config
        refs = _References()
        transport: StdioTransport | StreamableHttpTransport | SseTransport
        auth: NoAuth | BearerTokenAuth | ApiKeyAuth = NoAuth()
        if config.transport == "stdio":
            if _server_mode():
                raise StdioRefused(
                    "stdio MCP servers run a command on the host and are not available "
                    "in server mode"
                )
            if not config.command:
                raise ValueError("stdio transport requires 'command'")
            transport = StdioTransport(
                command=config.command,
                args=tuple(config.args),
                secret_env={k: refs(f"env/{k}", str(v)) for k, v in config.env.items()},
            )
        elif config.transport in ("http", "sse"):
            if not config.url:
                raise ValueError(f"{config.transport.upper()} transport requires 'url'")
            kind = StreamableHttpTransport if config.transport == "http" else SseTransport
            transport = kind(url=config.url)
            if config.auth_type == "bearer" and config.auth_token:
                auth = BearerTokenAuth(token=refs("auth_token", config.auth_token))
            elif config.auth_type == "api_key" and config.auth_token:
                auth = ApiKeyAuth(
                    key=refs("auth_token", config.auth_token), header=config.auth_header_name
                )
        else:
            raise ValueError(f"unknown transport: {config.transport}")
        settings = McpServerSettings(
            name=conduit_name(config.name),
            transport=transport,
            auth=auth,
            task_classes=frozenset({TASK_CLASS}),
            connect_timeout=config.timeout,
            call_timeout=config.timeout,
            list_timeout=config.timeout,
        )
        return settings, _Secrets(refs.held, self._secrets)

    def _http_transport(self) -> httpx2.AsyncBaseTransport | None:
        """SPARK's HTTP transport, when headers or certificate checks need one."""
        if self._config.transport == "stdio":
            return None
        extra = self._extra_headers()
        if not extra and self._config.ssl_verify:
            return None
        # verify=False only when the administrator turned certificate checks off.
        inner = httpx2.AsyncHTTPTransport(verify=bool(self._config.ssl_verify))
        return _HeaderTransport(inner, extra) if extra else inner

    # -- Lifecycle -----------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to the MCP server. Returns True on success, False on any failure."""
        if self.connected:
            return True
        await self.disconnect(quiet=True)
        transport = self._config.transport
        try:
            settings, secrets = self.settings()
        except StdioRefused as exc:
            logger.error("MCP server '%s' refused: %s", self.name, exc)
            return False
        except (ValidationError, ValueError) as exc:
            logger.error("MCP server '%s' (%s) is not usable: %s", self.name, transport, exc)
            return False
        connection = McpConnection(settings, secrets=secrets, http_transport=self._http_transport())
        try:
            await connection.connect()
        except McpError as exc:
            logger.error(
                "MCP server '%s' (%s) connection failed: %s",
                self.name,
                transport,
                exc.failure.value,
            )
            return False
        except ConduitError as exc:
            logger.error("MCP server '%s' (%s) connection failed: %s", self.name, transport, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - any failure is a failed connection
            logger.error(
                "MCP server '%s' (%s) connection failed: %s",
                self.name,
                transport,
                type(exc).__name__,
            )
            return False
        self._connection = connection
        logger.info("Connected to MCP server %s (%s)", self.name, transport)
        return True

    async def disconnect(self, *, quiet: bool = False) -> None:
        """Disconnect from the MCP server (conduit's close never raises)."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()
        if not quiet:
            logger.info("Disconnected from MCP server %s", self.name)

    # -- Tools ---------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """List tools available on this server."""
        connection = self._connection
        if connection is None or not connection.connected:
            return []
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "server": self.name,
            }
            for tool in await connection.list_tools()
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on this server; its result as text, unframed (see the module)."""
        connection = self._connection
        if connection is None or not connection.connected:
            raise RuntimeError(f"Not connected to {self.name}")
        failure: str | None = None
        try:
            result = await connection.call_tool(tool_name, arguments)
        except McpError as exc:
            failure = exc.failure.value
        if failure is not None:
            raise RuntimeError(f"MCP server '{self.name}' could not complete the call ({failure})")
        return {
            "content": [{"type": "text", "text": result.text}],
            "isError": result.is_error,
        }
