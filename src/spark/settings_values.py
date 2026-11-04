"""Typed reads of SPARK's settings.

konfig returns a ``SPARK__`` environment override as the string it was given:
``SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=false`` reads as ``'false'``,
which a plain truthiness test or ``bool()`` treats as on. Every boolean read
from konfig goes through :func:`setting_bool`, which takes a real boolean or
one of the usual spellings and refuses anything else, naming the key.
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["SettingValueError", "SettingsLike", "coerce_bool", "setting_bool"]

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


class SettingsLike(Protocol):
    """Anything that reads a dotted settings key with a default (konfig, the instance view)."""

    def get(self, key: str, default: Any = None) -> Any: ...


class SettingValueError(ValueError):
    """A setting holds a value that cannot be read as its type.

    The message names the key and the expected spellings, never the value, so a
    setting mistakenly given a secret does not echo it into a log.
    """

    def __init__(self, key: str, value: object) -> None:
        self.key = key
        super().__init__(
            f"setting {key} must be a boolean (true/false, 1/0, yes/no or on/off), "
            f"not a {type(value).__name__} with another value"
        )


def coerce_bool(value: object, key: str, default: bool) -> bool:
    """``value`` read as a boolean for ``key``; ``None`` (not set) gives ``default``.

    Accepts ``True`` and ``False``, the integers 1 and 0, and the strings
    true/false, 1/0, yes/no and on/off in any case with surrounding space.
    Raises :class:`SettingValueError` for anything else, including an empty string.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
    raise SettingValueError(key, value)


def setting_bool(settings: SettingsLike, key: str, default: bool) -> bool:
    """The boolean setting ``key`` from ``settings``, or ``default`` when it is not set."""
    return coerce_bool(settings.get(key, default), key, default)
