"""warden's housekeeping as worker duties (server mode).

:func:`register` adds to the worker's registration point
(:mod:`spark.worker`), so only the active worker (one per database, by the
worker's advisory lock) does any of it:

* ``warden-identity-pruning``: warden's ``identity.pruning(every_seconds=...)``
  held open as a service, deleting expired refresh tokens, single-use tokens,
  MFA challenges and sign-in throttle windows;
* ``warden-sso-pruning``: ``identity.sso.prune()`` as a periodic job, deleting
  expired single sign-on transactions and handoff codes;
* ``warden-audit-purge``: only when ``warden.audit.retention_days`` is set,
  warden's audit retention purge (``warden.audit.purge``), which deletes rows
  older than the retention behind a signed checkpoint. The purge needs
  ``DELETE`` on the audit table, which an application role may not hold
  (``warden.audit.grants``). With ``worker.audit_purge_database_url`` set
  (:func:`spark.worker.audit_purge_database`), each purge opens its own
  short-lived connection with it, as warden's purger role, and runs there with
  its own audit recorder and key store; the worker's own role can then stay
  append-only. Unset, the purge runs on the worker's connection, as before. A
  refused purge is logged (by the exception's type only, never the URL) and
  tried again next interval.

Both pruning duties run every ``worker.identity_prune_seconds`` (an hour by
default); the purge every ``worker.audit_purge_seconds`` (a day by default).
Each duty uses the warden runtime the worker builds while it is active
(``WorkerContext.identity``).
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, Protocol

from warden.audit import ActionRegistry, AuditRecorder, purge
from warden.audit.retention import CheckpointSigner, PurgeResult
from warden.keys import KeyStore

from spark import worker
from spark.identity.runtime import IdentityRuntime
from spark.mode import DatabaseSettings

__all__ = [
    "AUDIT_PURGE",
    "AuditPurge",
    "DEFAULT_PRUNE_SECONDS",
    "DEFAULT_PURGE_SECONDS",
    "IDENTITY_PRUNING",
    "SSO_PRUNING",
    "purge",
    "purge_audit",
    "prune_sso",
    "register",
]

logger = logging.getLogger(__name__)

IDENTITY_PRUNING: Final = "warden-identity-pruning"
SSO_PRUNING: Final = "warden-sso-pruning"
AUDIT_PURGE: Final = "warden-audit-purge"

DEFAULT_PRUNE_SECONDS: Final = 3600.0
DEFAULT_PURGE_SECONDS: Final = 86400.0


class SettingsLike(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...


def _seconds(settings: SettingsLike, key: str, default: float) -> float:
    try:
        value = float(settings.get(key, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _runtime(ctx: worker.WorkerContext) -> IdentityRuntime:
    if ctx.identity is None:
        raise worker.WorkerRefused("warden's pruning needs the worker's warden runtime")
    return ctx.identity


def _identity_pruning(every_seconds: float) -> worker.ServiceFactory:
    def factory(ctx: worker.WorkerContext) -> AbstractAsyncContextManager[Any]:
        return _runtime(ctx).identity.pruning(every_seconds=every_seconds)

    return factory


async def prune_sso(ctx: worker.WorkerContext) -> None:
    """Delete expired single sign-on transactions and handoff codes."""
    removed = await _runtime(ctx).identity.sso.prune()
    if removed:
        logger.info("Pruned %d expired single sign-on records", removed)


async def purge_audit(
    ctx: worker.WorkerContext, purge_database: DatabaseSettings | None = None
) -> None:
    """Purge audit rows older than ``warden.audit.retention_days`` (nothing when unset).

    With ``purge_database`` the purge runs on a connection of its own opened
    with it (the purger role); without, on the worker's.
    """
    runtime = _runtime(ctx)
    days = runtime.config.audit.retention_days
    if days is None:
        return
    if purge_database is None:
        result = await purge(
            runtime.recorder, runtime.keys, retention_days=days, actor=ctx.worker_id
        )
    else:
        result = await _purge_on(purge_database, ctx, runtime, days)
    if result.purged_rows:
        logger.info(
            "Purged %d audit rows through row %s (checkpoint %s)",
            result.purged_rows,
            result.through_id,
            result.checkpoint_id,
        )


async def _purge_on(
    settings: DatabaseSettings, ctx: worker.WorkerContext, runtime: IdentityRuntime, days: int
) -> PurgeResult:
    """The purge over its own pool on ``settings``, closed again when it is done."""
    from spark.db import Database
    from spark.identity.db import warden_database

    database = await Database.open(settings, min_size=1, max_size=2)
    try:
        wdb = warden_database(database)
        # Its own recorder (not installed process-wide) appends the checkpoint
        # on this connection; purge registers the action it needs.
        recorder = AuditRecorder(
            wdb, ActionRegistry(), details_max_bytes=runtime.config.audit.details_max_bytes
        )
        secrets = getattr(ctx.app, "secrets", None)
        keys: CheckpointSigner = (
            KeyStore(wdb, secrets, runtime.config.keys) if secrets is not None else runtime.keys
        )
        return await purge(recorder, keys, retention_days=days, actor=ctx.worker_id)
    finally:
        await database.close()


class AuditPurge:
    """The ``warden-audit-purge`` job: :func:`purge_audit` with its connection settings."""

    def __init__(self, database: DatabaseSettings | None = None) -> None:
        self.database = database

    async def __call__(self, ctx: worker.WorkerContext) -> None:
        await purge_audit(ctx, self.database)


def register(settings: SettingsLike, *, purge_database: DatabaseSettings | None = None) -> None:
    """Register warden's pruning (and the audit purge, if a retention is set); idempotent.

    ``purge_database`` is the purge's own connection
    (``worker.audit_purge_database_url``); None keeps it on the worker's.
    """
    every = _seconds(settings, "worker.identity_prune_seconds", DEFAULT_PRUNE_SECONDS)
    if IDENTITY_PRUNING not in {s.name for s in worker.registered_services()}:
        worker.register_service(IDENTITY_PRUNING, _identity_pruning(every))
    periodic = {job.name for job in worker.registered_periodic()}
    if SSO_PRUNING not in periodic:
        worker.register_periodic(SSO_PRUNING, every, prune_sso)
    if settings.get("warden.audit.retention_days") is not None and AUDIT_PURGE not in periodic:
        worker.register_periodic(
            AUDIT_PURGE,
            _seconds(settings, "worker.audit_purge_seconds", DEFAULT_PURGE_SECONDS),
            AuditPurge(purge_database),
        )
