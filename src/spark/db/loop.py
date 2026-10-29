"""Event loops for the data layer.

:func:`new_event_loop` returns a loop psycopg can use (the Selector loop on
Windows). :class:`LoopThread` runs such a loop in a daemon thread, for processes
that have no loop of their own to own the database pool: the tray daemon and
its scheduler, and tests.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

__all__ = ["LoopThread", "new_event_loop"]

T = TypeVar("T")


def new_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


class LoopThread:
    """An event loop running for ever in a daemon thread, until :meth:`stop`."""

    def __init__(self, name: str = "spark-db-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("the loop thread has not been started")
        return self._loop

    def start(self) -> asyncio.AbstractEventLoop:
        if self._thread is not None:
            raise RuntimeError("the loop thread is already started")
        loop = new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        self._loop = loop
        self._thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread.start()
        ready.wait()
        return loop

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine on the loop from another thread and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        if not loop.is_running():
            loop.close()
        self._thread = None
