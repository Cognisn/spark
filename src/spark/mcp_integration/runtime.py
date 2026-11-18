"""The MCP manager on the process's conduit loop (M2, F1).

An MCP session is bound to the loop its connection was opened on. Every MCP
session in a process lives on the one conduit loop the connection runtime owns
(:attr:`spark.connections.runtime.ConnectionRuntime.loop`, a
:class:`conduit.LoopThread`): there is no MCP loop thread of its own. Synchronous
callers (start-up, the settings follower, shutdown) go through conduit's sync
facade, :meth:`conduit.LoopThread.run`, which refuses to be called on the loop
itself; async callers on another loop are carried there by the manager
(:func:`spark.mcp_integration.manager.run_on`). The conversation manager hands
its listings and calls to :attr:`MCPRuntime.loop` as before.

A process with no connection runtime (a test, a script) gets a conduit loop
owned by this runtime and closed with it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Iterable
from typing import TypeVar

from conduit import LoopThread

from spark.mcp_integration.manager import MCPManager, MCPServerConfig

__all__ = ["MCPRuntime"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_APPLY_SECONDS = 120.0


async def _running_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


class MCPRuntime:
    """An MCP manager whose sessions live on a conduit loop."""

    def __init__(self, manager: MCPManager, conduit_loop: LoopThread, *, owns_loop: bool) -> None:
        self.manager = manager
        self.conduit_loop = conduit_loop
        self._owns_loop = owns_loop
        self.loop: asyncio.AbstractEventLoop = conduit_loop.run(_running_loop(), timeout=30)
        manager.bind(self.loop)

    @classmethod
    def start(cls, manager: MCPManager, conduit_loop: LoopThread | None = None) -> MCPRuntime:
        """Bind ``manager`` to ``conduit_loop`` (nothing is connected yet).

        ``conduit_loop`` is the process's (the connection runtime's); without one
        the runtime starts a conduit loop of its own and closes it on :meth:`stop`.
        """
        owns = conduit_loop is None
        loop = conduit_loop if conduit_loop is not None else LoopThread(name="spark-conduit")
        return cls(manager, loop, owns_loop=owns)

    def run(self, coro: Awaitable[T], timeout: float = 60.0) -> T:
        """Run ``coro`` on the conduit loop through conduit's sync facade and wait for it."""
        return self.conduit_loop.run(coro, timeout=timeout)

    def connect_all(self, timeout: float = 60.0) -> dict[str, bool]:
        """Connect every registered server and fill the tool cache.

        A server that cannot be reached at start-up is logged and left
        disconnected; the others connect, and start-up goes on.
        """

        async def connect() -> dict[str, bool]:
            results = await self.manager.connect_all()
            tools = await self.manager.list_all_tools(permissions=None)
            connected = sum(1 for ok in results.values() if ok)
            failed = sorted(name for name, ok in results.items() if not ok)
            logger.info("MCP servers: %d connected, %d failed", connected, len(failed))
            if failed:
                logger.warning("MCP servers not reachable at start-up: %s", ", ".join(failed))
            logger.debug("MCP tool cache: %d tools visible unscoped", len(tools))
            return results

        return self.run(connect(), timeout)

    def apply(self, configs: Iterable[MCPServerConfig]) -> set[str]:
        """Make the connected servers match ``configs`` (the follower calls this)."""
        return self.run(self.manager.reconcile(list(configs)), _APPLY_SECONDS)

    def stop(self, timeout: float = 15.0) -> None:
        """Disconnect every server, and close the conduit loop when this runtime owns it."""
        try:
            self.run(self.manager.disconnect_all(), timeout)
        except Exception as e:  # noqa: BLE001 - shutting down regardless
            logger.warning("MCP servers did not disconnect cleanly: %s", type(e).__name__)
        if self._owns_loop:
            self.conduit_loop.close()
