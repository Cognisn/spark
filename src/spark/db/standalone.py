"""A database for a process that has no event loop of its own (the tray daemon, the scheduler).

:func:`open_standalone` prepares the schema (migrating automatically in personal
mode, checking only in server mode), starts a :class:`~spark.db.loop.LoopThread`,
opens the :class:`~spark.db.engine.Database` on it and returns the bridge.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from spark.db import migrate
from spark.db.bridge import SyncDatabase
from spark.db.engine import Database
from spark.db.loop import LoopThread
from spark.mode import DatabaseSettings

__all__ = ["StandaloneDatabase", "open_standalone"]

_counter = itertools.count(1)


@dataclass
class StandaloneDatabase:
    """The bridge to a database whose loop runs in its own thread; call :meth:`close`."""

    sync_db: SyncDatabase
    _loop_thread: LoopThread
    loop_thread_name: str
    _closed: bool = field(default=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop_thread.run(self.sync_db.database.close(), timeout=30)
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
