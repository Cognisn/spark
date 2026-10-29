"""The single audit hook for settings changes.

Every write of an instance setting, a user preference or a secret calls
:func:`record` once it has succeeded. Auditing itself is out of scope for 0.3.0
wave 2: wave 3 installs a hook that sends these events to warden. An event never
carries the value, so a secret cannot leak through it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

__all__ = ["AuditHook", "SettingChange", "record", "set_audit_hook"]

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

_hook: AuditHook | None = None


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
