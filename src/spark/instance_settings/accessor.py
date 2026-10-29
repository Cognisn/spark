"""One accessor for reading a setting, whatever the mode.

* Personal mode: :class:`FileSettings` reads ``config.yaml`` through konfig.
* Server mode: :class:`ServerSettings` holds the administrator's instance settings
  from the database in memory (refreshed by the listener), and reads start-up keys
  from konfig.

Code calls :func:`get` once :func:`configure` has installed the source for the
mode; per-user preferences come from :func:`preferences`. Secrets are never read
here: they belong to :mod:`spark.instance_settings.secrets`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from spark import mode
from spark.db.engine import Executor
from spark.instance_settings import audit, registry, repository
from spark.instance_settings.audit import SettingChange
from spark.instance_settings.registry import InvalidSettingError, Scope, SettingsError

__all__ = [
    "FileSettings",
    "KonfigLike",
    "ServerSettings",
    "SettingsSource",
    "configure",
    "current",
    "get",
    "preferences",
    "reset",
    "set_preference",
]

logger = logging.getLogger(__name__)

_MISSING = object()


class KonfigLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


class SettingsSource(Protocol):
    def get(self, key: str) -> Any: ...


def _non_secret(key: str) -> registry.SettingSpec:
    spec = registry.spec(key)
    if spec.secret:
        raise SettingsError(f"{key} is a secret; read it through the secrets store")
    return spec


def _from_konfig(settings: KonfigLike, key: str) -> Any:
    spec = _non_secret(key)
    raw = settings.get(key, _MISSING)
    if raw is _MISSING or raw is None:
        return spec.default
    try:
        return registry.coerce(key, raw)
    except InvalidSettingError as exc:
        logger.warning("Using the default for %s: %s", key, exc)
        return spec.default


class FileSettings:
    """Personal mode: every setting from konfig (``config.yaml``, environment, defaults)."""

    def __init__(self, settings: KonfigLike) -> None:
        self._settings = settings

    def get(self, key: str) -> Any:
        return _from_konfig(self._settings, key)


class ServerSettings:
    """Server mode: instance settings cached from the database, start-up keys from konfig.

    Bound to the event loop of ``db``. :meth:`get` is safe from any thread (the
    cache is replaced, never mutated). :meth:`reload` re-reads every row; the
    listener calls it on each notification and after every reconnect.
    """

    def __init__(self, db: Executor, startup: KonfigLike) -> None:
        self._db = db
        self._startup = startup
        self._values: dict[str, Any] = {}
        self._observers: list[Callable[[], None]] = []
        self.generation = 0

    def get(self, key: str) -> Any:
        spec = _non_secret(key)
        if spec.scope is Scope.STARTUP:
            return _from_konfig(self._startup, key)
        if spec.scope is Scope.USER:
            return spec.default
        return self._values.get(key, spec.default)

    def on_change(self, observer: Callable[[], None]) -> None:
        """Call ``observer`` after every reload or change (for caches built from settings)."""
        self._observers.append(observer)

    def _changed(self, values: dict[str, Any]) -> None:
        self._values = values
        self.generation += 1
        for observer in list(self._observers):
            try:
                observer()
            except Exception:  # noqa: BLE001 - one consumer must not stop the others
                logger.exception("A settings observer failed")

    async def reload(self) -> None:
        self._changed(await repository.load_instance(self._db))

    async def set(self, key: str, value: Any, *, actor: str) -> Any:
        stored = await repository.save_instance(self._db, key, value, actor=actor)
        self._changed({**self._values, key: stored})
        audit.record(SettingChange(key, Scope.INSTANCE.value, actor, "set", False))
        return stored

    async def clear(self, key: str, *, actor: str) -> None:
        await repository.delete_instance(self._db, key, actor=actor)
        self._changed({k: v for k, v in self._values.items() if k != key})
        audit.record(SettingChange(key, Scope.INSTANCE.value, actor, "clear", False))


# -- The process-wide accessor --------------------------------------------------------

_source: SettingsSource | None = None


def configure(source: SettingsSource) -> None:
    """Install the settings source for this process (once, at start-up)."""
    global _source
    _source = source


def reset() -> None:
    """Remove the source (tests)."""
    global _source
    _source = None


def current() -> SettingsSource:
    if _source is None:
        raise RuntimeError("the settings accessor has not been configured")
    return _source


def get(key: str) -> Any:
    """The value of a non-secret setting, whatever the mode."""
    return current().get(key)


async def preferences(db: Executor | None, user_guid: str) -> dict[str, Any]:
    """Every preference of one user, defaults filled in.

    Server mode reads ``user_preferences`` through ``db``; personal mode (one
    local user) reads the config file through the configured source.
    """
    keys = registry.keys(Scope.USER)
    if mode.is_server():
        if db is None:
            raise ValueError("server mode needs the database to read preferences")
        stored = await repository.load_preferences(db, user_guid)
        return {k: stored.get(k, registry.spec(k).default) for k in keys}
    source = current()
    return {k: source.get(k) for k in keys}


async def set_preference(db: Executor, user_guid: str, key: str, value: Any) -> Any:
    """Server mode: store one preference for one user and report it to the audit hook."""
    stored = await repository.save_preference(db, user_guid, key, value)
    audit.record(SettingChange(key, Scope.USER.value, user_guid, "set", False, user_guid))
    return stored
