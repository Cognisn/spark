"""Repository for instance settings and user preferences (``instance_settings``, ``user_preferences``).

Values are validated against the registry before they are written and stored as
JSON text. Secret keys are refused outright: the database never holds a secret.
On PostgreSQL every change to ``instance_settings`` sends ``NOTIFY`` on
:data:`CHANNEL` inside the same transaction, so other web replicas hear of it
only once it has committed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from spark.db.dialect import upsert
from spark.db.engine import Executor
from spark.instance_settings import registry
from spark.instance_settings.registry import Scope, SettingsError
from spark.mode import Dialect

__all__ = [
    "CHANNEL",
    "REVISION",
    "delete_instance",
    "delete_preference",
    "load_instance",
    "load_preferences",
    "save_instance",
    "save_preference",
]

logger = logging.getLogger(__name__)

CHANNEL = "spark_instance_settings"
REVISION = "instance_settings_0001"

_INSTANCE_UPSERT = upsert(
    "instance_settings", ["key", "value", "updated_at", "updated_by"], ["key"]
)
_PREFERENCE_UPSERT = upsert(
    "user_preferences", ["user_guid", "key", "value", "updated_at"], ["user_guid", "key"]
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _checked(key: str, value: Any, scope: Scope) -> Any:
    """Validate ``value`` for a non-secret key of ``scope``; return its stored form."""
    spec = registry.spec(key)
    if spec.secret:
        raise SettingsError(f"{key} is a secret; secrets are written only through konfig")
    if spec.scope is not scope:
        raise SettingsError(f"{key} is a {spec.scope.value} setting, not a {scope.value} one")
    return registry.coerce(key, value)


def _decoded(key: str, text: str, scope: Scope) -> tuple[bool, Any]:
    try:
        return True, _checked(key, json.loads(text), scope)
    except (SettingsError, ValueError) as exc:
        logger.warning("Ignoring stored setting %s: %s", key, exc)
        return False, None


async def _notify(db: Executor, key: str) -> None:
    if db.dialect is Dialect.POSTGRESQL:
        await db.execute("SELECT pg_notify(?, ?)", (CHANNEL, key))


# -- Instance settings ------------------------------------------------------------


async def load_instance(db: Executor) -> dict[str, Any]:
    """Every stored instance setting that is still registered and valid."""
    rows = await db.fetch_all("SELECT key, value FROM instance_settings")
    loaded: dict[str, Any] = {}
    for row in rows:
        key = str(row["key"])
        ok, value = _decoded(key, str(row["value"]), Scope.INSTANCE)
        if ok:
            loaded[key] = value
    return loaded


async def save_instance(db: Executor, key: str, value: Any, *, actor: str) -> Any:
    """Store one instance setting and notify the other replicas; return the stored value."""
    stored = _checked(key, value, Scope.INSTANCE)
    async with db.transaction() as conn:
        await conn.execute(_INSTANCE_UPSERT, (key, json.dumps(stored), _now(), actor))
        await _notify(conn, key)
    return stored


async def delete_instance(db: Executor, key: str, *, actor: str) -> None:
    """Remove a stored instance setting (it reverts to its default) and notify."""
    _checked(key, registry.spec(key).default, Scope.INSTANCE)
    del actor  # recorded by the audit hook; the row is gone
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM instance_settings WHERE key = ?", (key,))
        await _notify(conn, key)


# -- User preferences ---------------------------------------------------------------


def _user(user_guid: str) -> str:
    if not user_guid:
        raise ValueError("a user is required")
    return user_guid


async def load_preferences(db: Executor, user_guid: str) -> dict[str, Any]:
    """The stored preferences of one user that are still registered and valid."""
    rows = await db.fetch_all(
        "SELECT key, value FROM user_preferences WHERE user_guid = ?", (_user(user_guid),)
    )
    loaded: dict[str, Any] = {}
    for row in rows:
        key = str(row["key"])
        ok, value = _decoded(key, str(row["value"]), Scope.USER)
        if ok:
            loaded[key] = value
    return loaded


async def save_preference(db: Executor, user_guid: str, key: str, value: Any) -> Any:
    """Store one preference for one user; return the stored value."""
    user = _user(user_guid)
    stored = _checked(key, value, Scope.USER)
    await db.execute(_PREFERENCE_UPSERT, (user, key, json.dumps(stored), _now()))
    return stored


async def delete_preference(db: Executor, user_guid: str, key: str) -> None:
    await db.execute(
        "DELETE FROM user_preferences WHERE user_guid = ? AND key = ?", (_user(user_guid), key)
    )
