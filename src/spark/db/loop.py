"""Event loops for the data layer.

:func:`new_event_loop` returns a loop psycopg can use (the Selector loop on
Windows). :class:`LoopThread` runs such a loop in a daemon thread, for processes
that have no loop of their own to own the database pool: the tray daemon and
its scheduler, and tests.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import traceback
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = ["LoopThread", "StopReport", "new_event_loop"]

T = TypeVar("T")

log = logging.getLogger(__name__)


def new_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


@dataclass(frozen=True)
class StopReport:
    """What :meth:`LoopThread.stop` found: whether the thread ended in time, and the
    tasks still pending on the loop that it cancelled."""

    stopped: bool
    cancelled: tuple[str, ...] = ()


def _describe(task: asyncio.Task[Any]) -> str:
    coro = task.get_coro()
    name = getattr(coro, "__qualname__", None) or repr(coro)
    frames = task.get_stack(limit=1)
    where = f" at {frames[0].f_code.co_filename}:{frames[0].f_lineno}" if frames else ""
    return f"{task.get_name()} {name}{where}"


# How long the stopped loop gives cancelled tasks to finish their CancelledError.
_CANCEL_GRACE = 2.0


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> list[str]:
    """On the stopped loop's own thread: cancel every task not done, give them a bounded
    moment to finish, and return their descriptions. Each waiter in another thread
    (``run_coroutine_threadsafe``) is then told the task was cancelled."""
    tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not tasks:
        return []
    described = [_describe(task) for task in tasks]
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.wait(tasks, timeout=_CANCEL_GRACE))
    loop.run_until_complete(asyncio.sleep(0))  # deliver the done callbacks
    return described


class LoopThread:
    """An event loop running for ever in a daemon thread, until :meth:`stop`."""

    def __init__(self, name: str = "spark-db-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._cancelled: list[str] = []

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

        cancelled: list[str] = []
        self._cancelled = cancelled

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
                cancelled.extend(_cancel_pending(loop))
            finally:
                loop.close()

        self._loop = loop
        self._thread = threading.Thread(target=_run, name=self._name, daemon=True)
        self._thread.start()
        ready.wait()
        return loop

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run a coroutine on the loop from another thread and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def where(self) -> str:
        """The loop thread's current stack, for a diagnostic ("" when it is not running)."""
        thread = self._thread
        if thread is None or thread.ident is None:
            return ""
        frame = sys._current_frames().get(thread.ident)
        return "".join(traceback.format_stack(frame)) if frame is not None else ""

    def pending(self) -> tuple[str, ...]:
        """The tasks not yet done on the loop, described, for a diagnostic."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return ()
        try:
            tasks = list(asyncio.all_tasks(loop))
        except RuntimeError:  # the set changed while this thread read it
            return ()
        return tuple(_describe(task) for task in tasks if not task.done())

    def stop(self, timeout: float = 10.0) -> StopReport:
        """Cancel what is still pending on the loop, stop it, and wait up to ``timeout``.

        Returns a :class:`StopReport`. Cancelled tasks, and a loop thread that did not
        end in time (with the stack that holds it), are logged as warnings.
        """
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return StopReport(stopped=True)
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        stopped = not thread.is_alive()
        found = tuple(self._cancelled) if stopped else ()
        if found:
            log.warning(
                "loop thread %s stopped with %d pending task(s) cancelled: %s",
                self._name,
                len(found),
                "; ".join(found),
            )
        if not stopped:
            log.warning(
                "loop thread %s did not stop within %g s; it is at:\n%s",
                self._name,
                timeout,
                self.where(),
            )
        self._thread = None
        return StopReport(stopped=stopped, cancelled=found)
