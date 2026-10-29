"""Async data layer for SQLite (personal mode) and PostgreSQL (server mode).

* :class:`Database` owns the connections and is bound to one event loop.
* :class:`Connection` is one connection inside a transaction; pass it to
  :meth:`Database.transaction` to join the caller's transaction.
* Repositories take an :class:`Executor` (a Database or a Connection).
* :class:`SyncDatabase` is the one bridge for synchronous code on worker threads.
"""

from spark.db.bridge import BridgeError, SyncDatabase
from spark.db.engine import Connection, Database, DatabaseError, Executor, Params, Row
from spark.db.loop import LoopThread, new_event_loop

__all__ = [
    "BridgeError",
    "Connection",
    "Database",
    "DatabaseError",
    "Executor",
    "LoopThread",
    "Params",
    "Row",
    "SyncDatabase",
    "new_event_loop",
]
