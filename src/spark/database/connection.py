"""Thread-safe database connection management."""

from __future__ import annotations

import logging
import threading
from typing import Any

from spark.database.backends import DatabaseBackend

logger = logging.getLogger(__name__)


class DatabaseConnection:
    """Manages a thread-safe database connection with reentrant locking."""

    def __init__(self, backend: DatabaseBackend) -> None:
        self._backend = backend
        self._conn: Any = None
        self._lock = threading.RLock()

    @property
    def backend(self) -> DatabaseBackend:
        return self._backend

    def get_connection(self) -> Any:
        """Get or create the database connection."""
        if self._conn is None:
            self._conn = self._backend.connect()
            logger.info("Database connection established")
        return self._conn

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        """Execute a SQL statement with thread safety."""
        with self._lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor

    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        """Execute a SQL statement for multiple parameter sets."""
        with self._lock:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.executemany(sql, params_list)

    def commit(self) -> None:
        """Commit the current transaction."""
        with self._lock:
            if self._conn:
                self._conn.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        with self._lock:
            if self._conn:
                self._conn.rollback()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
                logger.info("Database connection closed")

    @property
    def placeholder(self) -> str:
        return self._backend.placeholder()
