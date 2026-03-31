"""Database backend abstraction for multiple database engines."""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DatabaseBackend(ABC):
    """Abstract base for database backends."""

    @abstractmethod
    def connect(self) -> Any:
        """Return a new database connection."""

    @abstractmethod
    def placeholder(self) -> str:
        """SQL parameter placeholder for this backend (e.g. '?' or '%s')."""

    @abstractmethod
    def autoincrement(self) -> str:
        """SQL syntax for auto-increment primary key column."""

    def supports_returning(self) -> bool:
        """Whether the backend supports RETURNING clause."""
        return False

    @abstractmethod
    def upsert_sql(self, table: str, columns: list[str], conflict_columns: list[str]) -> str:
        """Generate an UPSERT statement."""


class SQLiteBackend(DatabaseBackend):
    """SQLite database backend."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def placeholder(self) -> str:
        return "?"

    def autoincrement(self) -> str:
        return "INTEGER PRIMARY KEY AUTOINCREMENT"

    def upsert_sql(self, table: str, columns: list[str], conflict_columns: list[str]) -> str:
        ph = self.placeholder()
        cols = ", ".join(columns)
        vals = ", ".join([ph] * len(columns))
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in conflict_columns)
        conflict = ", ".join(conflict_columns)
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({vals}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        )


class MySQLBackend(DatabaseBackend):
    """MySQL / MariaDB backend."""

    def __init__(self, host: str, port: int, database: str, user: str, password: str) -> None:
        self._config = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
        }

    def connect(self) -> Any:
        import mysql.connector  # type: ignore[import-untyped]

        return mysql.connector.connect(**self._config)

    def placeholder(self) -> str:
        return "%s"

    def autoincrement(self) -> str:
        return "INTEGER PRIMARY KEY AUTO_INCREMENT"

    def upsert_sql(self, table: str, columns: list[str], conflict_columns: list[str]) -> str:
        ph = self.placeholder()
        cols = ", ".join(columns)
        vals = ", ".join([ph] * len(columns))
        updates = ", ".join(
            f"{c} = VALUES({c})" for c in columns if c not in conflict_columns
        )
        return f"INSERT INTO {table} ({cols}) VALUES ({vals}) ON DUPLICATE KEY UPDATE {updates}"


class PostgreSQLBackend(DatabaseBackend):
    """PostgreSQL backend."""

    def __init__(self, host: str, port: int, database: str, user: str, password: str) -> None:
        self._config = {
            "host": host,
            "port": port,
            "dbname": database,
            "user": user,
            "password": password,
        }

    def connect(self) -> Any:
        import psycopg2  # type: ignore[import-untyped]

        return psycopg2.connect(**self._config)

    def placeholder(self) -> str:
        return "%s"

    def autoincrement(self) -> str:
        return "SERIAL PRIMARY KEY"

    def supports_returning(self) -> bool:
        return True

    def upsert_sql(self, table: str, columns: list[str], conflict_columns: list[str]) -> str:
        ph = self.placeholder()
        cols = ", ".join(columns)
        vals = ", ".join([ph] * len(columns))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in conflict_columns)
        conflict = ", ".join(conflict_columns)
        return (
            f"INSERT INTO {table} ({cols}) VALUES ({vals}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        )


class MSSQLBackend(DatabaseBackend):
    """Microsoft SQL Server backend."""

    def __init__(self, host: str, port: int, database: str, user: str, password: str) -> None:
        self._config = {
            "server": f"{host},{port}",
            "database": database,
            "uid": user,
            "pwd": password,
        }

    def connect(self) -> Any:
        import pyodbc  # type: ignore[import-untyped]

        parts = ";".join(f"{k}={v}" for k, v in self._config.items())
        conn_str = f"DRIVER={{ODBC Driver 17 for SQL Server}};{parts}"
        return pyodbc.connect(conn_str)

    def placeholder(self) -> str:
        return "?"

    def autoincrement(self) -> str:
        return "INTEGER IDENTITY(1,1) PRIMARY KEY"

    def upsert_sql(self, table: str, columns: list[str], conflict_columns: list[str]) -> str:
        # MSSQL uses MERGE for upsert — simplified here
        ph = self.placeholder()
        cols = ", ".join(columns)
        vals = ", ".join([ph] * len(columns))
        return f"INSERT INTO {table} ({cols}) VALUES ({vals})"


def create_backend(settings: object) -> DatabaseBackend:
    """Factory: create a database backend from settings."""
    get = settings.get  # type: ignore[union-attr]
    db_type = str(get("database.type", "sqlite")).lower()

    if db_type == "sqlite":
        # SQLite always uses the platform data directory
        from spark.core.application import _get_data_path

        db_path = _get_data_path() / "spark.db"
        return SQLiteBackend(str(db_path))
    elif db_type in ("mysql", "mariadb"):
        return MySQLBackend(
            host=get("database.host", "localhost"),
            port=int(get("database.port", 3306)),
            database=get("database.name", "spark"),
            user=get("database.user", ""),
            password=get("database.password", ""),
        )
    elif db_type == "postgresql":
        return PostgreSQLBackend(
            host=get("database.host", "localhost"),
            port=int(get("database.port", 5432)),
            database=get("database.name", "spark"),
            user=get("database.user", ""),
            password=get("database.password", ""),
        )
    elif db_type == "mssql":
        return MSSQLBackend(
            host=get("database.host", "localhost"),
            port=int(get("database.port", 1433)),
            database=get("database.name", "spark"),
            user=get("database.user", ""),
            password=get("database.password", ""),
        )
    else:
        raise ValueError(f"Unsupported database type: {db_type}")
