"""MCP server management: connection, tool discovery, and execution.

Each server is reached through conduit's MCP client over the official SDK
(:class:`~spark.mcp_integration.client.MCPClient`), on the process's conduit loop
(:meth:`MCPManager.bind`). SPARK keeps its policy above conduit (M2): which
servers a caller may use (asked of :mod:`spark.tools.registry`), the audit of
every call, and the untrusted-content handling of every result (A16, in the
conversation manager).

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
import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from spark.mcp_integration.client import MCPClient, MCPServerConfig, StdioRefused
from spark.mcp_integration.oauth import SecretsLike, SecretsTokenStore, SignIns, token_store

if TYPE_CHECKING:
    from conduit import SecretResolver

logger = logging.getLogger(__name__)

__all__ = [
    "MCPClient",
    "MCPManager",
    "MCPServerConfig",
    "MCPToolRefused",
    "StdioRefused",
    "UNSCOPED",
    "run_on",
]

T = TypeVar("T")


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


async def run_on(loop: asyncio.AbstractEventLoop | None, coro: Coroutine[Any, Any, T]) -> T:
    """Await ``coro`` on ``loop`` (conduit's), from whichever loop the caller is on.

    An MCP session belongs to the loop it was opened on, so every session lives
    on the process's conduit loop; a caller on another loop (a request handler)
    has its work carried there and awaits the result.
    """
    if loop is None or loop.is_closed():
        return await coro
    try:
        current: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - always awaited inside a loop
        current = None
    if current is loop:
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


def _konfig_store(resolver: Any) -> SecretsLike | None:
    """The konfig secrets store behind conduit's konfig resolver, if ``resolver`` is one.

    Start-up gives the manager only the resolver; its store also holds the OAuth
    tokens, so the manager reads it from there.
    """
    try:
        from conduit.integrations.konfig import KonfigSecretResolver
    except ImportError:  # pragma: no cover - conduit ships the konfig integration
        return None
    if not isinstance(resolver, KonfigSecretResolver):
        return None
    store = getattr(resolver, "_secrets", None)
    if store is None or not all(
        callable(getattr(store, m, None)) for m in ("get", "set", "delete")
    ):
        return None
    return store  # type: ignore[no-any-return]


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
    """Manages multiple MCP server connections.

    Its sessions live on one loop, the process's conduit loop (:meth:`bind`, done by
    :class:`~spark.mcp_integration.runtime.MCPRuntime`): every coroutine method may
    be awaited from any loop and runs there. ``secrets`` is SPARK's resolver over
    konfig, given to each client for credentials configured as references.

    ``tokens`` is konfig's secrets store, which holds each OAuth server's tokens
    (:mod:`spark.mcp_integration.oauth`); without it, the store behind a konfig
    ``secrets`` resolver is used. :attr:`sign_ins` holds the pending OAuth sign-ins.
    """

    def __init__(
        self,
        client_factory: Callable[[MCPServerConfig], ClientLike] | None = None,
        *,
        secrets: SecretResolver | None = None,
        tokens: SecretsLike | None = None,
    ) -> None:
        self._secrets = secrets
        self._token_secrets: SecretsLike | None = (
            tokens if tokens is not None else _konfig_store(secrets)
        )
        self.sign_ins = SignIns()
        self._client_factory: Callable[[MCPServerConfig], ClientLike] = (
            client_factory if client_factory is not None else self._new_client
        )
        self._clients: dict[str, ClientLike] = {}
        self._tools_cache: list[dict[str, Any]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _new_client(self, config: MCPServerConfig) -> ClientLike:
        return MCPClient(config, secrets=self._secrets, tokens=self.token_store(config.name))

    def token_store(self, name: str) -> SecretsTokenStore | None:
        """The OAuth token store of server ``name`` (``None`` without a secrets store)."""
        if self._token_secrets is None:
            return None
        return token_store(self._token_secrets, name)

    @property
    def token_secrets(self) -> SecretsLike | None:
        """The secrets store holding the OAuth tokens."""
        return self._token_secrets

    def new_client(self, config: MCPServerConfig) -> ClientLike:
        """A client for ``config`` made as the manager makes its own (not registered)."""
        return self._client_factory(config)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Keep every session on ``loop`` (conduit's), whoever awaits the manager."""
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The loop the sessions live on, once bound."""
        return self._loop

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
        """Whether a caller with ``permissions`` may use server ``name``.

        Asked of SPARK's tool decision point,
        :func:`spark.tools.registry.is_mcp_server_available`.
        """
        from spark.tools.registry import is_mcp_server_available

        return is_mcp_server_available(name, permissions=permissions)

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
        """Connect to all registered servers. Returns name→success mapping.

        A server that cannot be reached is reported as ``False`` and the others
        still connect; it is tried again when its settings next change.
        """
        return await run_on(self._loop, self._connect_all(progress_callback))

    async def _connect_all(
        self, progress_callback: Callable[[str, bool], Any] | None
    ) -> dict[str, bool]:
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
        await run_on(self._loop, self._disconnect_all())

    async def _disconnect_all(self) -> None:
        for client in self._clients.values():
            await client.disconnect()
        self._tools_cache = None

    async def reconcile(self, configs: Iterable[MCPServerConfig]) -> set[str]:
        """Make the registered servers match ``configs``; returns the names that changed.

        A new or changed server is (re)connected, a removed or disabled one
        disconnected; unchanged servers keep their connection. The tool cache is
        rebuilt.
        """
        return await run_on(self._loop, self._reconcile(list(configs)))

    async def _reconcile(self, configs: list[MCPServerConfig]) -> set[str]:
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
        await self._discover()
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
        tools = await run_on(self._loop, self._discover())
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
        return await run_on(
            self._loop,
            self._call_tool(
                tool_name, arguments, server_name, permissions, user_guid, conversation_id
            ),
        )

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        server_name: str | None,
        permissions: Scope,
        user_guid: str | None,
        conversation_id: Any,
    ) -> dict[str, Any]:

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
            elif _server_mode() and not isinstance(permissions, (set, frozenset, list, tuple)):
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

    # -- Administration (the settings endpoints) ------------------------------------

    async def connect_server(self, config: MCPServerConfig) -> bool:
        """Register ``config`` in place of any server of that name and connect it."""
        return await run_on(self._loop, self._connect_server(config))

    async def _connect_server(self, config: MCPServerConfig) -> bool:
        if config.name in self._clients:
            await self._drop(config.name)
        self.add_server(config)
        client = self._clients.get(config.name)
        connected = await client.connect() if client is not None else False
        self._tools_cache = None
        await self._discover()
        return connected

    async def set_enabled(self, name: str, enabled: bool) -> None:
        """Connect or disconnect a registered server, keeping it registered."""
        await run_on(self._loop, self._set_enabled(name, enabled))

    async def _set_enabled(self, name: str, enabled: bool) -> None:
        client = self._clients.get(name)
        if client is None:
            return
        if enabled and not client.connected:
            if await client.connect():
                logger.info("MCP server '%s' re-enabled and connected", name)
        elif not enabled and client.connected:
            await client.disconnect()
            logger.info("MCP server '%s' disabled and disconnected", name)
        self._tools_cache = None
        await self._discover()

    async def remove_server(self, name: str) -> None:
        """Disconnect and forget a server."""
        await run_on(self._loop, self._remove_server(name))

    async def _remove_server(self, name: str) -> None:
        if name in self._clients:
            await self._drop(name)
        self._tools_cache = None

    async def probe(self, client: ClientLike) -> list[dict[str, Any]] | None:
        """Connect ``client`` (not registered), list its tools and disconnect it.

        ``None`` when it does not connect. Runs on the manager's loop, as every
        session does.
        """
        return await run_on(self._loop, _probe(client))

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
    def from_config(
        cls,
        config: dict[str, Any],
        *,
        secrets: SecretResolver | None = None,
        tokens: SecretsLike | None = None,
    ) -> MCPManager:
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

        manager = cls(secrets=secrets, tokens=tokens)
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


async def _probe(client: ClientLike) -> list[dict[str, Any]] | None:
    if not await client.connect():
        return None
    try:
        return await client.list_tools()
    finally:
        await client.disconnect()
