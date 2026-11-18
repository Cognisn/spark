"""Health and readiness probes for orchestrators and load balancers.

* ``GET /health``: liveness. Answers 200 whenever the process serves; it depends
  on nothing (not the database, not the secrets store), so a load balancer's
  target check and a container health check never take a task out of service
  for a dependency outage.
* ``GET /ready``: readiness. Answers 200 only when the database answers, the
  schema is at both Alembic heads (SPARK's and warden's), and the secrets store
  can be read; otherwise 503. The body lists each check as ``ok`` or ``failed``
  with the SPARK version, and never a secret, URL, host name or stack trace.

Both are served without a session and outside warden's checks (the server's
authentication middleware lets :data:`PROBE_PATHS` through before it asks the
authenticator), in personal and server mode alike.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import spark

__all__ = ["PROBE_PATHS", "router"]

logger = logging.getLogger(__name__)

#: Paths answered without a session, before the authenticator is consulted.
PROBE_PATHS: Final = frozenset({"/health", "/ready"})

#: Each check is given this long; a hung dependency is a failed check.
CHECK_TIMEOUT_SECONDS: Final = 5.0

#: A readiness answer is reused for this long, so a burst of probes costs one check.
CACHE_SECONDS: Final = 2.0

#: The secret name the store is asked about; it need not exist (only the read matters).
_PROBE_SECRET: Final = "spark_readiness_probe"

_NO_STORE: Final = {"Cache-Control": "no-store"}

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    """Liveness: the process is serving."""
    return JSONResponse({"status": "ok", "version": spark.__version__}, headers=_NO_STORE)


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: database reachable, schema at both heads, secrets store readable."""
    state = request.app.state
    cached = getattr(state, "ready_cache", None)
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        body = cached[1]
    else:
        body = await _run_checks(request)
        state.ready_cache = (time.monotonic(), body)
    status = 200 if body["status"] == "ready" else 503
    return JSONResponse(body, status_code=status, headers=_NO_STORE)


async def _run_checks(request: Request) -> dict[str, Any]:
    checks = {
        "database": await _guarded("database", _check_database(request)),
        "schema": await _guarded("schema", _check_schema(request)),
        "secrets": await _guarded("secrets", _check_secrets(request)),
    }
    ok = all(value == "ok" for value in checks.values())
    return {
        "status": "ready" if ok else "not_ready",
        "version": spark.__version__,
        "checks": checks,
    }


async def _guarded(name: str, check: Any) -> str:
    """``ok`` or ``failed``; the reason goes to the log by exception type only."""
    try:
        await asyncio.wait_for(check, CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Readiness: the %s check timed out", name)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - any failure is "not ready"; never echoed
        logger.warning("Readiness: the %s check failed (%s)", name, type(exc).__name__)
        return "failed"
    return "ok"


async def _check_database(request: Request) -> None:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise RuntimeError("the database is not open")
    if await database.fetch_value("SELECT 1") != 1:
        raise RuntimeError("the database gave an unexpected answer")


async def _check_schema(request: Request) -> None:
    from spark.db import migrate

    settings = getattr(request.app.state, "database_settings", None)
    if settings is None:
        raise RuntimeError("no database is configured")
    # Both chains (SPARK's and warden's) are checked; read only.
    await asyncio.to_thread(migrate.check_at_head, settings)


async def _check_secrets(request: Request) -> None:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        raise RuntimeError("no secrets store is configured")
    secrets = ctx.secrets

    def read() -> None:
        # Drop any cached copy first, so a store that has become unreadable shows.
        refresh = getattr(secrets, "refresh", None)
        if callable(refresh):
            refresh()
        secrets.has(_PROBE_SECRET)

    await asyncio.to_thread(read)
