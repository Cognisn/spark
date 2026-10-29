"""Path containment: the one resolved-path check used by every file tool.

A path is first checked as text, with the same rules on every host, so that a
form that is harmless on one platform cannot become an escape on another:
backslash is a separator everywhere, and ``..`` components, rooted and drive
forms, network and device namespaces, reserved device names, alternate data
streams, trailing dots and spaces, and control characters are refused. The
path is then resolved (symlinks followed) and must lie inside the root.

:func:`contain` takes an area-relative key (server mode, the user's area).
:func:`contain_any` also accepts an absolute path inside one of several roots
(personal mode, the configured allowed paths).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

__all__ = ["PathRefused", "contain", "contain_any"]

_SEPARATORS = re.compile(r"[\\/]")
_DRIVE = re.compile(r"[A-Za-z]:")
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{n}" for n in "123456789"}
    | {f"LPT{n}" for n in "123456789"}
)


class PathRefused(ValueError):
    """A path that is not, or might not be, inside the permitted root."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Path refused: {reason}.")
        self.reason = reason


def _check_text(text: str, *, allow_absolute: bool) -> tuple[str | None, list[str]]:
    """Validate ``text`` as written; return its anchor (or None) and its parts."""
    if not text.strip():
        raise PathRefused("the path is empty")
    if any(ord(ch) < 32 for ch in text):
        raise PathRefused("the path contains a control character")
    if text[:2] in ("\\\\", "//", "\\/", "/\\"):
        raise PathRefused("network and device paths are not allowed")

    anchor: str | None = None
    body = text
    if _DRIVE.match(text):
        if text[2:3] not in ("\\", "/"):
            raise PathRefused("drive-relative paths are not allowed")
        if not allow_absolute or sys.platform != "win32":
            raise PathRefused("absolute paths are not allowed")
        anchor, body = text[:2] + "\\", text[3:]
    elif text[0] == "\\":
        raise PathRefused("rooted paths without a drive are not allowed")
    elif text[0] == "/":
        if not allow_absolute or sys.platform == "win32":
            raise PathRefused("absolute paths are not allowed")
        anchor, body = "/", text[1:]

    parts: list[str] = []
    for part in _SEPARATORS.split(body):
        if part in ("", "."):
            continue
        if not part.strip(" ."):
            raise PathRefused("parent directory components are not allowed")
        if part[-1] in " .":
            raise PathRefused("names ending in a dot or space are not allowed")
        if ":" in part:
            raise PathRefused("alternate data streams are not allowed")
        if part.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES:
            raise PathRefused("reserved device names are not allowed")
        parts.append(part)
    return anchor, parts


def _within(candidate: Path, roots: Sequence[Path]) -> Path:
    """The resolved-path check: ``candidate`` resolved must lie inside a resolved root."""
    resolved = candidate.resolve()
    for root in roots:
        if resolved.is_relative_to(root.resolve()):
            return resolved
    raise PathRefused("the path is outside the permitted area")


def contain(root: Path, key: str) -> Path:
    """Resolve an area-relative ``key`` inside ``root``, or raise :class:`PathRefused`."""
    _, parts = _check_text(key, allow_absolute=False)
    return _within(root.joinpath(*parts), [root])


def contain_any(roots: Sequence[Path], path: str) -> Path:
    """Resolve ``path`` inside one of ``roots``, or raise :class:`PathRefused`.

    An absolute path must lie inside one of the roots; a relative path is taken
    against the first.
    """
    if not roots:
        raise PathRefused("no permitted area is configured")
    anchor, parts = _check_text(path, allow_absolute=True)
    base = Path(anchor) if anchor is not None else roots[0]
    return _within(base.joinpath(*parts), roots)
