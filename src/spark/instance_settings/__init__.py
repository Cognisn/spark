"""Instance settings, user preferences, secrets and start-up configuration (A4, A8, A9).

* :mod:`.registry`: every setting key with its scope, default and validation.
* :mod:`.accessor`: :func:`get` reads a setting whatever the mode; :func:`preferences`.
* :mod:`.repository`: the ``instance_settings`` and ``user_preferences`` tables.
* :mod:`.listener`: LISTEN/NOTIFY propagation between web replicas.
* :mod:`.secrets`: the write-only secrets writer over konfig.
* :mod:`.startup`: start-up configuration, validated.
* :mod:`.access`: the administrator check (a stub until warden, wave 3).
* :mod:`.audit`: the single hook every settings change is reported to.

The web lifespan calls :func:`attach` once the database is open and
:func:`detach` before closing it.
"""

from __future__ import annotations

from typing import Any

from spark import mode
from spark.instance_settings.accessor import (
    FileSettings,
    KonfigLike,
    ServerSettings,
    configure,
    current,
    get,
    preferences,
    set_preference,
)
from spark.instance_settings.listener import SettingsListener, start_listener, stop_listener

__all__ = [
    "FileSettings",
    "ServerSettings",
    "SettingsListener",
    "attach",
    "configure",
    "current",
    "detach",
    "get",
    "preferences",
    "server_settings",
    "set_preference",
    "start_listener",
    "stop_listener",
]


async def attach(app: Any, database: Any, settings: KonfigLike) -> None:
    """Install the settings source for the mode on ``app.state`` and the accessor.

    Server mode: load the instance settings from ``database`` and start the
    listener. Personal mode: read ``config.yaml`` through konfig.
    """
    if mode.is_server():
        source = ServerSettings(database, settings)
        await source.reload()
        app.state.instance_settings = source
        app.state.settings_listener = await start_listener(source)
    else:
        app.state.instance_settings = FileSettings(settings)
        app.state.settings_listener = None
    configure(app.state.instance_settings)


async def detach(app: Any) -> None:
    """Stop the listener (server mode) before the database closes."""
    listener: SettingsListener | None = getattr(app.state, "settings_listener", None)
    if listener is not None:
        await stop_listener(listener)
    app.state.settings_listener = None


async def server_settings(app: Any, database: Any, settings: KonfigLike) -> ServerSettings:
    """The app's :class:`ServerSettings`, loaded on first use when :func:`attach` has not run.

    Without the listener a lazily loaded cache only sees this replica's changes;
    :func:`attach` from the lifespan is what makes changes reach every replica.
    """
    source = getattr(app.state, "instance_settings", None)
    if isinstance(source, ServerSettings):
        return source
    source = ServerSettings(database, settings)
    await source.reload()
    app.state.instance_settings = source
    configure(source)
    return source
