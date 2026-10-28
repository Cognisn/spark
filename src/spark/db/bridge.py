"""The one sanctioned route from synchronous code to the async database.

Spark's orchestrators (the chat turn and its tool loop, sub-agents, debate and
panel, the knowledge-graph builder, the scheduler) are synchronous and run on
worker threads. The database is async and bound to one event loop. A worker
thread reaches it through :class:`SyncDatabase`::

    sync_db.call(conversations.get_conversation, conversation_id, user_guid)

which schedules the repository coroutine on the database's loop and blocks the
calling thread until it finishes.

Rules:

* Never call it on the database loop's own thread (from a coroutine or a
  callback running on that loop): the loop would wait for itself for ever.
  :class:`BridgeError` is raised instead. Code on the loop awaits repositories
  directly; sync helpers called from async code go through ``asyncio.to_thread``.
* A unit of work needing several statements in one transaction is written as
  one repository coroutine, never as a connection held across bridged calls.
* Every call has a timeout (``timeout`` seconds, default 120): a hung database
  fails the turn instead of hanging the worker thread.

New code should be async and use the repositories directly.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate, ParamSpec, TypeVar

from spark.db.engine import Database, Params, Row

__all__ = ["BridgeError", "SyncDatabase"]

P = ParamSpec("P")
T = TypeVar("T")

DEFAULT_TIMEOUT = 120.0


class BridgeError(RuntimeError):
    """The bridge cannot run the call (wrong thread, or the loop has stopped)."""


class SyncDatabase:
    """Blocking access to a :class:`Database` for code on worker threads."""

    def __init__(self, database: Database, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._db = database
        self._timeout = timeout

    @property
    def database(self) -> Database:
        return self._db

    def call(
        self,
        fn: Callable[Concatenate[Database, P], Awaitable[T]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run ``fn(database, *args, **kwargs)`` on the database loop and return its result."""
        self._check_thread()

        async def _invoke() -> T:
            return await fn(self._db, *args, **kwargs)

        return self.run(_invoke())

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the database loop and wait for it (closing it if refused)."""
        try:
            self._check_thread()
        except BridgeError:
            coro.close()
            raise
        future = asyncio.run_coroutine_threadsafe(coro, self._db.loop)
        try:
            return future.result(self._timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"database call did not finish within {self._timeout:g} seconds"
            ) from None

    def fetch_one(self, sql: str, params: Params = ()) -> Row | None:
        return self.run(self._db.fetch_one(sql, params))

    def fetch_all(self, sql: str, params: Params = ()) -> list[Row]:
        return self.run(self._db.fetch_all(sql, params))

    def fetch_value(self, sql: str, params: Params = ()) -> Any:
        return self.run(self._db.fetch_value(sql, params))

    def execute(self, sql: str, params: Params = ()) -> int:
        return self.run(self._db.execute(sql, params))

    def _check_thread(self) -> None:
        loop = self._db.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise BridgeError(
                "SyncDatabase called on the database event loop thread; await the "
                "repository instead (or move the sync caller to a worker thread)"
            )
        if loop.is_closed() or not loop.is_running():
            raise BridgeError("the database event loop is not running")
