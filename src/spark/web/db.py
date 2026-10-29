"""Database access for endpoints.

Endpoints run on the event loop: they await repositories on :func:`database`.
Anything handed to a worker thread (a builder, an orchestrator, a sync helper)
gets :func:`sync_database`, the bridge, and is started with ``asyncio.to_thread``
or a thread of its own, never called directly on the loop.
"""

from __future__ import annotations

from fastapi import Request

from spark.db import Database, SyncDatabase

__all__ = ["database", "sync_database"]


def database(request: Request) -> Database:
    """The async database, opened by the app's lifespan on this event loop."""
    db: Database = request.app.state.database
    return db


def sync_database(request: Request) -> SyncDatabase:
    """The bridge for synchronous code running on worker threads."""
    sync_db: SyncDatabase = request.app.state.sync_db
    return sync_db
