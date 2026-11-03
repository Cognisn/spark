"""The persistent event loop MCP sessions live on, with the manager that owns them.

An MCP session is bound to the loop its connection was opened on, so the web
process runs one loop on its own thread for every MCP connection: the turn
threads dispatch tool listings and calls onto it (``mcp_loop``), and the
settings follower applies a change to ``mcp.servers`` there
(:meth:`MCPRuntime.apply`), from any thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine, Iterable
from typing import Any, TypeVar

from spark.mcp_integration.manager import MCPManager, MCPServerConfig

__all__ = ["MCPRuntime"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_APPLY_SECONDS = 120.0


class MCPRuntime:
    """An MCP manager and the loop thread its sessions run on."""

    def __init__(self, manager: MCPManager, loop: asyncio.AbstractEventLoop) -> None:
        self.manager = manager
        self.loop = loop
        self._thread: threading.Thread | None = None

    @classmethod
    def start(cls, manager: MCPManager) -> MCPRuntime:
        """Start the loop thread for ``manager`` (nothing is connected yet)."""
        loop = asyncio.new_event_loop()
        runtime = cls(manager, loop)

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        runtime._thread = threading.Thread(target=run, name="spark-mcp-loop", daemon=True)
        runtime._thread.start()
        return runtime

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 60.0) -> T:
        """Run ``coro`` on the MCP loop from another thread and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def connect_all(self, timeout: float = 60.0) -> dict[str, bool]:
        """Connect every registered server and fill the tool cache."""

        async def connect() -> dict[str, bool]:
            results = await self.manager.connect_all()
            tools = await self.manager.list_all_tools(permissions=None)
            connected = sum(1 for ok in results.values() if ok)
            logger.info("MCP servers: %d connected, %d failed", connected, len(results) - connected)
            logger.debug("MCP tool cache: %d tools visible unscoped", len(tools))
            return results

        return self.run(connect(), timeout)

    def apply(self, configs: Iterable[MCPServerConfig]) -> set[str]:
        """Make the connected servers match ``configs`` (the follower calls this)."""
        return self.run(self.manager.reconcile(list(configs)), _APPLY_SECONDS)

    def stop(self, timeout: float = 15.0) -> None:
        """Disconnect every server and stop the loop thread."""
        if self.loop.is_closed():
            return
        if self.loop.is_running():
            try:
                self.run(self.manager.disconnect_all(), timeout)
            except Exception as e:  # noqa: BLE001 - shutting down regardless
                logger.warning("MCP servers did not disconnect cleanly: %s", e)
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if not self.loop.is_running():
            self.loop.close()
