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
  (``warden.audit.grants``): a refused purge is logged and tried again next
  interval.

Both pruning duties run every ``worker.identity_prune_seconds`` (an hour by
default); the purge every ``worker.audit_purge_seconds`` (a day by default).
Each duty uses the warden runtime the worker builds while it is active
(``WorkerContext.identity``).
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, Protocol

from warden.audit import purge

from spark import worker
from spark.identity.runtime import IdentityRuntime

__all__ = [
    "AUDIT_PURGE",
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


async def purge_audit(ctx: worker.WorkerContext) -> None:
    """Purge audit rows older than ``warden.audit.retention_days`` (nothing when unset)."""
    runtime = _runtime(ctx)
    days = runtime.config.audit.retention_days
    if days is None:
        return
    result = await purge(runtime.recorder, runtime.keys, retention_days=days, actor=ctx.worker_id)
    if result.purged_rows:
        logger.info(
            "Purged %d audit rows through row %s (checkpoint %s)",
            result.purged_rows,
            result.through_id,
            result.checkpoint_id,
        )


def register(settings: SettingsLike) -> None:
    """Register warden's pruning (and the audit purge, if a retention is set); idempotent."""
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
            purge_audit,
        )
