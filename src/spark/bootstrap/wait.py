"""Web and worker wait for ``spark bootstrap`` rather than racing it or exiting at once.

In a deployment, web and worker may start together, and before the bootstrap task
has finished. Each checks for what bootstrap leaves behind and, while something is
missing, logs one line and checks again every ``bootstrap.wait_interval_seconds``
(5) for up to ``bootstrap.wait_seconds`` (600) in total, so start order alone never
trips a deployment's rollback. Past the limit it raises :class:`BootstrapNotReady`
naming what is missing.

What each waits for, on a short-lived connection of its own for each check (its
restricted login may not exist until bootstrap creates it):

* the worker (:func:`before_worker`): a working database login, the instance
  identity (warden's signing key, the TOTP key and the CSRF key), which it may not
  be able to write (its role may only read the secrets store), and the bootstrap
  record when the installation names a first administrator (``bootstrap.admin.email``);
* web (:func:`before_web_serves`), when the installation names a first
  administrator: the login, the record and the identity, before uvicorn starts.
  While it waits, a small listener on the web port answers ``GET /health`` with 200
  and ``GET /ready`` (and anything else) with 503, so a load balancer's or
  container's health check keeps passing; once bootstrap is complete the listener
  closes and uvicorn takes the port. Without a first administrator configured, web
  does not wait and creates any missing key itself, as before, so an installation
  that does not run ``spark bootstrap`` keeps working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

import psycopg

import spark
from spark.bootstrap import settings as bootstrap_settings
from spark.db.engine import Database, Executor
from spark.instance_settings.startup import StartupConfigError
from spark.mode import DatabaseSettings

__all__ = [
    "BootstrapNotReady",
    "before_web_serves",
    "before_worker",
    "missing",
    "missing_at",
    "probe_answer",
    "wait_until_ready",
]

logger = logging.getLogger(__name__)

LOGIN: Final = "a working database login (spark bootstrap creates restricted logins)"
RECORD: Final = "the bootstrap record (run spark bootstrap after spark migrate)"
SIGNING_KEY: Final = "a usable warden signing key"
#: How long the probe listener waits for a request's head before closing the connection.
_REQUEST_TIMEOUT: Final = 5.0
_REQUEST_LIMIT: Final = 8192

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
Check = Callable[[], Awaitable[list[str]]]


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


async def missing_at(
    database: DatabaseSettings, settings: Any, secrets: Any, *, record: bool
) -> list[str]:
    """:func:`missing`, on a connection of its own; :data:`LOGIN` when it cannot connect."""
    try:
        probe = await psycopg.AsyncConnection.connect(
            database.url or "", autocommit=True, connect_timeout=10
        )
    except psycopg.Error:
        return [LOGIN]
    await probe.close()
    db = await Database.open(database, min_size=1, max_size=2)
    try:
        return await missing(db, settings, secrets, record=record)
    finally:
        await db.close()


async def _poll(check: Check, settings: Any, *, role: str, sleep: Sleep, clock: Clock) -> None:
    total, interval = bootstrap_settings.wait_limits(settings)
    deadline = clock() + total
    waited = False
    while True:
        gaps = await check()
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
    """Return once nothing is :func:`missing` on ``database``; :class:`BootstrapNotReady` after the limit."""

    async def check() -> list[str]:
        return await missing(database, settings, secrets, record=record)

    await _poll(check, settings, role=role, sleep=sleep, clock=clock)


async def before_worker(
    database: DatabaseSettings,
    settings: Any,
    secrets: Any,
    *,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> None:
    """The worker's wait, before any duty starts."""
    record = bootstrap_settings.configured(settings)

    async def check() -> list[str]:
        return await missing_at(database, settings, secrets, record=record)

    await _poll(check, settings, role="worker", sleep=sleep, clock=clock)


# -- web: probes answered while waiting ----------------------------------------------


def _response(status: int, reason: str, body: dict[str, Any]) -> bytes:
    payload = json.dumps(body).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        "Cache-Control: no-store\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
    )
    if status == 503:
        head += "Retry-After: 5\r\n"
    return (head + "\r\n").encode("ascii") + payload


def probe_answer(request_line: bytes) -> bytes:
    """The listener's answer to one request line: 200 for ``GET /health``, else 503."""
    parts = request_line.decode("latin-1").split()
    method = parts[0] if parts else ""
    path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""
    version = spark.__version__
    if method in ("GET", "HEAD") and path == "/health":
        return _response(200, "OK", {"status": "ok", "version": version})
    if path == "/ready":
        body: dict[str, Any] = {
            "status": "not_ready",
            "version": version,
            "checks": {"bootstrap": "waiting"},
        }
        return _response(503, "Service Unavailable", body)
    return _response(503, "Service Unavailable", {"status": "starting", "version": version})


async def _serve_probe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _REQUEST_TIMEOUT)
        writer.write(probe_answer(head[:_REQUEST_LIMIT].split(b"\r\n", 1)[0]))
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    except OSError:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def before_web_serves(
    database: DatabaseSettings,
    settings: Any,
    secrets: Any,
    *,
    host: str,
    port: int,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> None:
    """Web's wait, before uvicorn binds ``host:port``; a no-op without bootstrap settings.

    The first check runs before anything listens, so a server already bootstrapped
    starts at once. Otherwise the probe listener answers on ``host:port`` until
    bootstrap is complete (or :class:`BootstrapNotReady`), then closes.
    """
    if not bootstrap_settings.configured(settings):
        return

    async def check() -> list[str]:
        return await missing_at(database, settings, secrets, record=True)

    first = await check()
    if not first:
        return
    answered = [first]

    async def later() -> list[str]:
        if answered:
            return answered.pop()
        return await check()

    server = await asyncio.start_server(_serve_probe, host, port, limit=_REQUEST_LIMIT)
    logger.info("Answering /health on %s:%d while spark bootstrap finishes", host, port)
    try:
        await _poll(later, settings, role="web", sleep=sleep, clock=clock)
    finally:
        server.close()
        await server.wait_closed()
