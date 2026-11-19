"""Web and worker wait for ``spark bootstrap`` rather than racing it or exiting at once.

In a deployment, web and worker may start together, and before the bootstrap task
has finished. Each checks for what bootstrap leaves behind and, while something is
missing, logs one line and checks again every ``bootstrap.wait_interval_seconds``
(5) for up to ``bootstrap.wait_seconds`` (600) in total, so start order alone never
trips a deployment's rollback. Past the limit it raises :class:`BootstrapNotReady`
naming what is missing.

What each waits for:

* the worker: the instance identity (warden's signing key, the TOTP key and the CSRF
  key), which it may not be able to write (its role may only read the secrets
  store), and the bootstrap record when the installation names a first
  administrator (``bootstrap.admin.email``);
* web, when the installation names a first administrator: the bootstrap record and
  the identity. Without one, web creates any missing key itself, as before, so an
  installation that does not run ``spark bootstrap`` keeps working.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from spark.bootstrap import settings as bootstrap_settings
from spark.db.engine import Database, Executor
from spark.instance_settings.startup import StartupConfigError
from spark.mode import DatabaseSettings

__all__ = [
    "BootstrapNotReady",
    "before_web",
    "before_worker",
    "missing",
    "wait_until_ready",
]

logger = logging.getLogger(__name__)

RECORD: Final = "the bootstrap record (run spark bootstrap after spark migrate)"
SIGNING_KEY: Final = "a usable warden signing key"

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class BootstrapNotReady(StartupConfigError):
    """Bootstrap had not finished within the wait; the message names what is missing."""


async def _record_missing(database: Executor) -> bool:
    from spark.bootstrap.run import completed

    return not await completed(database)


async def missing(database: Database, settings: Any, secrets: Any, *, record: bool) -> list[str]:
    """What is not there yet: the record (when ``record``), the identity secrets, a signing key."""
    from warden.keys import KeyStore, KeyStoreError

    from spark.identity.db import warden_database
    from spark.identity.runtime import CSRF_KEY_SECRET, warden_config
    from spark.instance_settings.startup import public_base_url

    gaps: list[str] = []
    if record and await _record_missing(database):
        gaps.append(RECORD)
    base_url = public_base_url(settings.get("server.public_base_url", ""))
    config = warden_config(settings, secrets, base_url)
    for name in (config.identity.totp_key_secret, CSRF_KEY_SECRET):
        if not await asyncio.to_thread(secrets.get, name):
            gaps.append(f"the secret {name}")
    keys = KeyStore(warden_database(database), secrets, config.keys)
    try:
        await keys.sign(b"spark bootstrap check")
    except KeyStoreError:
        gaps.append(SIGNING_KEY)
    return gaps


async def wait_until_ready(
    database: Database,
    settings: Any,
    secrets: Any,
    *,
    role: str,
    record: bool,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> None:
    """Return once nothing is :func:`missing`; :class:`BootstrapNotReady` after the limit."""
    total, interval = bootstrap_settings.wait_limits(settings)
    deadline = clock() + total
    waited = False
    while True:
        gaps = await missing(database, settings, secrets, record=record)
        if not gaps:
            if waited:
                logger.info("Bootstrap is complete; spark %s is starting", role)
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise BootstrapNotReady(
                f"spark {role}: bootstrap was not complete after {total:g} s; missing: "
                f"{'; '.join(gaps)}"
            )
        if not waited:
            logger.warning(
                "spark %s is waiting for spark bootstrap (missing: %s); checking every %g s "
                "for up to %g s",
                role,
                "; ".join(gaps),
                interval,
                total,
            )
            waited = True
        await sleep(min(interval, remaining))


async def before_web(database: Database, settings: Any, secrets: Any) -> None:
    """Web start-up's wait, before warden's runtime starts (a no-op without bootstrap settings)."""
    if not bootstrap_settings.configured(settings):
        return
    await wait_until_ready(database, settings, secrets, role="web", record=True)


async def before_worker(
    database: DatabaseSettings,
    settings: Any,
    secrets: Any,
    *,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> None:
    """The worker's wait, on a short-lived pool of its own, before any duty starts."""
    db = await Database.open(database, max_size=2)
    try:
        await wait_until_ready(
            db,
            settings,
            secrets,
            role="worker",
            record=bootstrap_settings.configured(settings),
            sleep=sleep,
            clock=clock,
        )
    finally:
        await db.close()
