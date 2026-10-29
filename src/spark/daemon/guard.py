"""The tray daemon and the background daemon run only in personal mode.

In server mode scheduled actions run in the worker role (``spark worker``), one
active worker per database; a tray icon on a server would run them a second time.
"""

from __future__ import annotations

import asyncio

from spark.mode import Mode, RuntimeSettings

__all__ = ["SERVER_MODE_MESSAGE", "ServerModeRefused", "load_runtime", "refuse_server_mode"]

SERVER_MODE_MESSAGE = (
    "The Spark tray daemon runs only in personal mode. This configuration is in "
    "server mode, where scheduled actions run in the worker: start it with "
    "`spark worker` instead."
)


class ServerModeRefused(RuntimeError):
    """The daemon was started with a server-mode configuration."""


def refuse_server_mode(runtime: RuntimeSettings) -> None:
    if runtime.mode is Mode.SERVER:
        raise ServerModeRefused(SERVER_MODE_MESSAGE)


def load_runtime() -> RuntimeSettings:
    """Read the mode and database settings through konfig, as the web interface does."""
    import spark
    from spark.core import application

    async def _load() -> RuntimeSettings:
        from konfig import AppContext

        config_path = application._get_config_path()
        application._ensure_config(config_path)
        async with AppContext(
            name="Spark Daemon",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=application._default_settings(),
            env_prefix="SPARK",
        ) as ctx:
            return application.configure_runtime(ctx.settings)

    return asyncio.run(_load())
