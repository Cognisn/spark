"""The single audit hook for settings changes.

Every write of an instance setting, a user preference or a secret calls
:func:`record` once it has succeeded. In server mode :mod:`spark.identity.audit`
installs a hook that sends these events to warden. An event never carries the
value, so a secret cannot leak through it.

A write made in a database transaction (an instance setting) reports through
:func:`record_in_transaction` instead, inside that transaction, when a
transaction hook is installed: its audit row then commits or rolls back with
the write, and a failed audit fails the write.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from spark.db.engine import Connection

__all__ = [
    "AuditHook",
    "SettingChange",
    "TransactionHook",
    "record",
    "record_in_transaction",
    "set_audit_hook",
    "set_transaction_hook",
    "transaction_hook",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingChange:
    key: str
    scope: str
    actor: str
    action: Literal["set", "clear"]
    secret: bool
    user_guid: str | None = None


AuditHook = Callable[[SettingChange], None]
TransactionHook = Callable[["Connection", SettingChange], Awaitable[None]]

_hook: AuditHook | None = None
_transaction_hook: TransactionHook | None = None


def set_audit_hook(hook: AuditHook | None) -> None:
    """Install the hook every settings change is reported to (``None`` removes it)."""
    global _hook
    _hook = hook


def record(change: SettingChange) -> None:
    """Report a change that has already happened; a failing hook is logged, never raised."""
    logger.debug("Setting %s %s by %s", change.key, change.action, change.actor)
    if _hook is None:
        return
    try:
        _hook(change)
    except Exception:  # noqa: BLE001 - auditing must not undo a committed change
        logger.exception("The settings audit hook failed for %s", change.key)


def set_transaction_hook(hook: TransactionHook | None) -> None:
    """Install the hook a write calls inside its transaction (``None`` removes it)."""
    global _transaction_hook
    _transaction_hook = hook


def transaction_hook() -> TransactionHook | None:
    """The installed transaction hook, or None (writes then report through :func:`record`)."""
    return _transaction_hook


async def record_in_transaction(conn: Connection, change: SettingChange) -> None:
    """Report a change inside the transaction ``conn`` that makes it; raises if the hook fails.

    Without a transaction hook the change is reported through :func:`record`.
    """
    hook = _transaction_hook
    if hook is None:
        record(change)
        return
    await hook(conn, change)
