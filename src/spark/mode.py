"""Deployment mode: personal (SQLite, one local user) or server (PostgreSQL, multi-user).

The mode is read once at start-up from the konfig setting ``mode`` (environment
``SPARK__MODE``) and validated together with the database settings. The rest of
the code asks this module, through :func:`current_mode` or :func:`is_server`,
never the raw settings.

Personal mode keeps the SQLite database in the platform data directory. Server
mode requires ``database.url`` to be a PostgreSQL URL and refuses anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

__all__ = [
    "DatabaseSettings",
    "Dialect",
    "Mode",
    "ModeConfigError",
    "RuntimeSettings",
    "configure",
    "current",
    "current_mode",
    "is_server",
    "reset",
    "resolve",
]


class Mode(StrEnum):
    PERSONAL = "personal"
    SERVER = "server"


class Dialect(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


class ModeConfigError(ValueError):
    """The mode or database settings are invalid; Spark refuses to start."""


class SettingsLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


@dataclass(frozen=True)
class DatabaseSettings:
    dialect: Dialect
    url: str | None = None
    sqlite_path: Path | None = None


@dataclass(frozen=True)
class RuntimeSettings:
    mode: Mode
    database: DatabaseSettings


_REMOVED_TYPES = frozenset({"mysql", "mariadb", "mssql"})
_POSTGRES_SCHEMES = frozenset({"postgresql", "postgres", "postgresql+psycopg"})


def resolve(settings: SettingsLike, *, default_sqlite_path: Path) -> RuntimeSettings:
    """Read and validate the mode and database settings.

    Raises :class:`ModeConfigError` with a message safe to show (no credentials).
    """
    raw_mode = settings.get("mode", Mode.PERSONAL.value)
    if raw_mode is None:
        raw_mode = Mode.PERSONAL.value
    if not isinstance(raw_mode, str):
        raise ModeConfigError("mode must be 'personal' or 'server'")
    try:
        selected = Mode(raw_mode.strip().lower())
    except ValueError:
        raise ModeConfigError("mode must be 'personal' or 'server'") from None

    db_type = str(settings.get("database.type", "") or "").strip().lower()
    url = str(settings.get("database.url", "") or "").strip()

    if selected is Mode.SERVER:
        return RuntimeSettings(selected, _server_database(url))

    if db_type in _REMOVED_TYPES:
        raise ModeConfigError(
            f"database.type '{db_type}' is no longer supported: Spark uses SQLite in "
            "personal mode and PostgreSQL in server mode"
        )
    if db_type == Dialect.POSTGRESQL.value or url:
        raise ModeConfigError(
            "personal mode uses SQLite; to use PostgreSQL set mode to server mode "
            "with database.url"
        )
    return RuntimeSettings(
        selected, DatabaseSettings(Dialect.SQLITE, sqlite_path=default_sqlite_path)
    )


def _server_database(url: str) -> DatabaseSettings:
    if not url:
        raise ModeConfigError("server mode requires database.url (a PostgreSQL URL)")
    scheme = urlsplit(url).scheme.lower() if "://" in url else ""
    if scheme not in _POSTGRES_SCHEMES:
        raise ModeConfigError("server mode requires database.url to be a PostgreSQL URL")
    if scheme == "postgresql+psycopg":
        url = "postgresql" + url[len(scheme) :]
    return DatabaseSettings(Dialect.POSTGRESQL, url=url)


_current: RuntimeSettings | None = None


def configure(runtime: RuntimeSettings) -> None:
    """Set the process-wide runtime settings (called once at start-up)."""
    global _current
    _current = runtime


def reset() -> None:
    """Clear the runtime settings (tests only)."""
    global _current
    _current = None


def current() -> RuntimeSettings:
    if _current is None:
        raise RuntimeError("the Spark mode has not been configured")
    return _current


def current_mode() -> Mode:
    return current().mode


def is_server() -> bool:
    return current_mode() is Mode.SERVER
