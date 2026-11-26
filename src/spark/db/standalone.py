"""A database for a process that has no event loop of its own (the tray daemon, the scheduler).

:func:`open_standalone` prepares the schema (migrating automatically in personal
mode, checking only in server mode), starts a :class:`~spark.db.loop.LoopThread`,
opens the :class:`~spark.db.engine.Database` on it and returns the bridge.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from spark.db import migrate
from spark.db.bridge import SyncDatabase
from spark.db.engine import Database
from spark.db.loop import LoopThread
from spark.mode import DatabaseSettings

__all__ = ["StandaloneDatabase", "open_standalone"]

log = logging.getLogger(__name__)

_counter = itertools.count(1)

# A close normally takes milliseconds (seconds when the disk is stalled); past this it
# gives up, says what was still pending, and stops the loop thread (its own bound).
CLOSE_TIMEOUT = 15.0


@dataclass
class StandaloneDatabase:
    """The bridge to a database whose loop runs in its own thread; call :meth:`close`."""

    sync_db: SyncDatabase
    _loop_thread: LoopThread
    loop_thread_name: str
    _closed: bool = field(default=False)
    close_timeout: float = field(default=CLOSE_TIMEOUT)

    def close(self) -> None:
        """Close the database and stop its loop thread, within a bounded time.

        If the database does not close within ``close_timeout`` seconds, the loop
        thread's stack and the tasks pending on it are logged, the loop thread is
        stopped (cancelling them) and :class:`TimeoutError` is raised.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._loop_thread.run(self.sync_db.database.close(), timeout=self.close_timeout)
        except TimeoutError:
            log.warning(
                "database on %s did not close within %g s; pending: %s; the loop thread is at:\n%s",
                self.loop_thread_name,
                self.close_timeout,
                "; ".join(self._loop_thread.pending()) or "nothing",
                self._loop_thread.where(),
            )
            raise
        finally:
            self._loop_thread.stop()


def open_standalone(settings: DatabaseSettings, *, auto_migrate: bool) -> StandaloneDatabase:
    """Prepare the schema, then open the database on a new loop thread."""
    migrate.prepare(settings, auto_migrate=auto_migrate)
    name = f"spark-db-loop-{next(_counter)}"
    loop_thread = LoopThread(name=name)
    loop_thread.start()
    try:
        database: Database = loop_thread.run(Database.open(settings), timeout=60)
    except BaseException:
        loop_thread.stop()
        raise
    return StandaloneDatabase(SyncDatabase(database), loop_thread, name)
