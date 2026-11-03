"""Propagation of instance settings between web replicas over PostgreSQL LISTEN/NOTIFY (A8).

Each replica runs one :class:`SettingsListener`: a dedicated psycopg connection
(opened from the database settings in :mod:`spark.mode`, never one held out of
the pool) that runs ``LISTEN`` on :data:`spark.instance_settings.repository.CHANNEL`.
Every notification triggers a full reload of the replica's cache. The bound on
propagation is the time to deliver one notification and reload one small table.

When the connection drops the listener reconnects with exponential back-off and
reloads in full once it is listening again, so a change made while it was away
is never missed. While idle it checks the connection every ``idle_timeout``
seconds, so a silently dead connection is noticed within that time.

Secrets live in konfig's store, not the database (A9). A notification whose
payload names a secret (:func:`spark.instance_settings.secrets.announce`) first
has ``refresh_secrets`` re-read the store on a worker thread, then reloads, so
the consumers that hold a secret rebuild with the new value. The store is
refreshed after every connect too, for a secret changed while away.

Start and stop it with :func:`start_listener` and :func:`stop_listener`; the web
lifespan does so in server mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from spark import mode
from spark.instance_settings.accessor import ServerSettings
from spark.instance_settings.repository import CHANNEL
from spark.instance_settings.secrets import is_secret_key
from spark.mode import DatabaseSettings

__all__ = [
    "APPLICATION_NAME",
    "Connect",
    "SettingsListener",
    "default_connect",
    "start_listener",
    "stop_listener",
]

logger = logging.getLogger(__name__)

APPLICATION_NAME = "spark-settings-listener"

Connect = Callable[[str], Awaitable[Any]]


async def default_connect(url: str) -> Any:
    """A dedicated autocommit connection for LISTEN (not from the pool)."""
    import psycopg

    return await psycopg.AsyncConnection.connect(
        url, autocommit=True, application_name=APPLICATION_NAME
    )


class SettingsListener:
    """Listens for settings changes and calls ``reload`` after each, and after every connect.

    ``refresh_secrets`` (blocking; run on a worker thread) runs before the reload
    after every connect and on every notification that names a secret.
    """

    def __init__(
        self,
        url: str,
        reload: Callable[[], Awaitable[None]],
        *,
        refresh_secrets: Callable[[], None] | None = None,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        idle_timeout: float = 5.0,
        connect: Connect | None = None,
    ) -> None:
        if not url:
            raise ValueError("the settings listener needs a PostgreSQL URL")
        self._url = url
        self._reload = reload
        self._refresh_secrets = refresh_secrets
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._idle_timeout = idle_timeout
        self._connect: Connect = connect or default_connect
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._ever_connected = False
        self.connected = asyncio.Event()
        self.reconnects = 0

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="spark-settings-listener"
            )

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.connected.clear()

    async def _run(self) -> None:
        delay = self._reconnect_delay
        while not self._stopping:
            try:
                conn = await self._connect(self._url)
            except Exception as exc:  # noqa: BLE001 - keep trying until stopped
                logger.warning("Settings listener cannot connect (retrying): %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
                continue
            try:
                await conn.execute(f"LISTEN {CHANNEL}")
                if self._ever_connected:
                    self.reconnects += 1
                    logger.info("Settings listener reconnected; reloading all settings")
                self._ever_connected = True
                await self._refresh()
                await self._reload()
                self.connected.set()
                delay = self._reconnect_delay
                await self._listen(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                logger.warning("Settings listener lost its connection: %s", exc)
            finally:
                self.connected.clear()
                with contextlib.suppress(Exception):
                    await conn.close()
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _listen(self, conn: Any) -> None:
        while not self._stopping:
            async for notify in conn.notifies(timeout=self._idle_timeout):
                if is_secret_key(str(notify.payload)):
                    await self._refresh()
                await self._reload()
            await conn.execute("SELECT 1")  # notice a connection that died while idle

    async def _refresh(self) -> None:
        """Have the secrets store re-read its data; a failure must not stop the reload."""
        if self._refresh_secrets is None:
            return
        try:
            await asyncio.to_thread(self._refresh_secrets)
        except Exception:  # noqa: BLE001 - the settings still reload; the next notice retries
            logger.exception("Refreshing the secrets store failed")


async def start_listener(
    settings: ServerSettings,
    database: DatabaseSettings | None = None,
    *,
    wait: float | None = 10.0,
    **options: Any,
) -> SettingsListener:
    """Start a listener that keeps ``settings`` current; wait for its first full load.

    ``database`` defaults to the process's mode settings. Raises
    :class:`TimeoutError` (and stops the listener) if it is not listening within
    ``wait`` seconds; ``wait=None`` returns at once.
    """
    db_settings = database or mode.current().database
    listener = SettingsListener(db_settings.url or "", settings.reload, **options)
    listener.start()
    if wait is not None:
        try:
            await asyncio.wait_for(listener.connected.wait(), wait)
        except TimeoutError:
            await listener.stop()
            raise TimeoutError("the settings listener could not connect in time") from None
    return listener


async def stop_listener(listener: SettingsListener) -> None:
    """Stop the listener and close its connection (safe to call twice)."""
    await listener.stop()
