"""Starting the connection runtime in a process, and the process's one runtime.

:func:`start` is called once a process has configured its mode and opened its
database (the web process's start-up after ``configure_runtime``, the worker, the
personal tray daemon): in personal mode it first converts ``config.yaml``'s 0.2
providers into connections (M12, once); on a server it lists, once, the retired
``providers.*`` and ``models.offered`` settings it ignores. It then builds the
runtime, with SPARK's secret resolver over konfig (A9) and its usage sink, applies
the stored configuration, and installs the runtime as the process's own.

:func:`current` is the process's runtime. :func:`ensure` gives code with no
start-up of its own (an action run in the tray daemon or the worker) the process's
runtime, starting it on first use and re-reading the configuration at most every
:data:`STALE_AFTER_S` where no settings follower keeps it current.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

from conduit import ProviderFactory, SecretResolver
from conduit.integrations.konfig import KonfigSecretResolver

from spark.connections.runtime import ConnectionRuntime, Snapshot
from spark.connections.store import DatabaseConnectionStore, FileConnectionStore, load_snapshot
from spark.connections.upgrade import ignored_server_settings, upgrade_config_file
from spark.connections.usage import UsageSink, database_recorder
from spark.db.engine import Executor

__all__ = [
    "STALE_AFTER_S",
    "Refresher",
    "current",
    "ensure",
    "install",
    "loader",
    "reset",
    "start",
]

logger = logging.getLogger(__name__)

#: How long a runtime with no settings follower trusts its configuration.
STALE_AFTER_S: Final = 30.0


class _Bridge(Protocol):
    def call(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any: ...


class _SecretsStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


def loader(sync_db: _Bridge, *, server: bool, config_path: Path | None) -> Callable[[], Snapshot]:
    """A function reading the stored connection configuration, from any worker thread."""
    if server:

        async def load_server(db: Executor) -> Snapshot:
            return await load_snapshot(db, DatabaseConnectionStore(db), server=True)

        return lambda: sync_db.call(load_server)
    if config_path is None:
        raise ValueError("personal mode keeps its connections in config.yaml: give its path")
    store = FileConnectionStore(config_path)

    async def load_personal(db: Executor) -> Snapshot:
        return await load_snapshot(db, store, server=False)

    return lambda: sync_db.call(load_personal)


class Refresher:
    """The settings follower's target: re-read the configuration and apply it."""

    def __init__(self, runtime: ConnectionRuntime, load: Callable[[], Snapshot]) -> None:
        self._runtime = runtime
        self._load = load

    def refresh(self) -> None:
        self._runtime.refresh(self._load)


_lock = threading.Lock()
_runtime: ConnectionRuntime | None = None
_load: Callable[[], Snapshot] | None = None
_followed = False
_loaded_at = 0.0


def install(
    runtime: ConnectionRuntime,
    load: Callable[[], Snapshot] | None = None,
    *,
    followed: bool = False,
) -> None:
    """Make ``runtime`` the process's own (closing any earlier one).

    ``followed`` says a settings follower keeps it current, so :func:`ensure` never
    re-reads the configuration itself.
    """
    global _runtime, _load, _followed, _loaded_at
    with _lock:
        old, _runtime, _load = _runtime, runtime, load
        _followed, _loaded_at = followed, time.monotonic()
    if old is not None and old is not runtime:
        old.close()


def current() -> ConnectionRuntime | None:
    """The process's runtime, or None before one is started."""
    with _lock:
        return _runtime


def reset() -> None:
    """Close and forget the process's runtime (shutdown and tests)."""
    global _runtime, _load
    with _lock:
        old, _runtime, _load = _runtime, None, None
    if old is not None:
        old.close()


def start(
    *,
    secrets: _SecretsStore,
    sync_db: _Bridge,
    server: bool,
    config_path: Path | None = None,
    factories: Sequence[ProviderFactory] | None = None,
    resolver: SecretResolver | None = None,
    followed: bool = False,
) -> tuple[ConnectionRuntime, Callable[[], Snapshot]]:
    """Start the process's connection runtime and install it. Returns it and its loader."""
    if server:
        ignored = sync_db.call(ignored_server_settings)
        if ignored:
            logger.warning(
                "Ignoring %d stored setting(s) that named connections replace: %s",
                len(ignored),
                ", ".join(ignored),
            )
    elif config_path is not None:
        upgrade_config_file(config_path, secrets)
    runtime = ConnectionRuntime(
        secrets=resolver if resolver is not None else KonfigSecretResolver(secrets),
        usage=UsageSink(database_recorder(sync_db)),
        factories=factories,
    )
    load = loader(sync_db, server=server, config_path=config_path)
    try:
        runtime.apply(load())
    except BaseException:
        runtime.close()
        raise
    failed = runtime.failures()
    running = sorted(c.id for c in runtime.connections() if c.enabled and c.id not in failed)
    logger.info("Model connections ready: %s", ", ".join(running) or "none")
    install(runtime, load, followed=followed)
    return runtime, load


def ensure(
    *, secrets: _SecretsStore, sync_db: _Bridge, server: bool, config_path: Path | None
) -> ConnectionRuntime:
    """The process's runtime, started now if need be and refreshed when stale."""
    global _loaded_at
    with _lock:
        runtime, load, followed, loaded_at = _runtime, _load, _followed, _loaded_at
    if runtime is None:
        runtime, _ = start(secrets=secrets, sync_db=sync_db, server=server, config_path=config_path)
        return runtime
    if not followed and load is not None and time.monotonic() - loaded_at > STALE_AFTER_S:
        runtime.refresh(load)
        with _lock:
            _loaded_at = time.monotonic()
    return runtime
