"""Credentials in bundle content: found by pattern, and redacted on export.

A bundle never carries a secret (A15). Export replaces, wherever it occurs in
text, each secret value the exporting instance holds and anything shaped like a
well-known credential (:data:`PATTERNS`); a file whose content holds one is
left out. Import scans every member's bytes for the same shapes and refuses the
bundle on any hit, so a bundle edited by hand cannot bring one in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Final, Protocol

__all__ = ["PATTERNS", "REDACTED", "find", "find_in_stream", "redact"]

REDACTED: Final = "[redacted credential]"

# Name -> the shape of a credential, as bytes (the scan reads raw member bytes).
PATTERNS: Final[dict[str, re.Pattern[bytes]]] = {
    "anthropic or openai key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "aws access key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "google api key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}"),
    "xai key": re.compile(rb"\bxai-[A-Za-z0-9]{20,}"),
    "github token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    "slack token": re.compile(rb"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "elevenlabs key": re.compile(rb"\bsk_[a-f0-9]{48}\b"),
    "warden api key": re.compile(rb"\bwdn_[A-Za-z0-9_-]{16,}"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


class Readable(Protocol):
    def read(self, n: int = -1, /) -> bytes: ...


_TEXT_PATTERNS: Final = [re.compile(p.pattern.decode("ascii")) for p in PATTERNS.values()]
# Secret values shorter than this are too common to replace safely (and are not credentials).
_MIN_SECRET = 8
# A chunked scan keeps this much of the previous chunk, so no credential spans unseen.
_OVERLAP = 512


def find(data: bytes) -> list[str]:
    """The kinds of credential found in ``data`` (empty when none)."""
    return [name for name, pattern in PATTERNS.items() if pattern.search(data)]


def find_in_stream(stream: Readable, chunk_size: int = 1 << 20) -> Iterator[str]:
    """:func:`find` over a stream read in chunks; yields each kind found (possibly repeated)."""
    tail = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return
        window = tail + chunk
        yield from find(window)
        tail = window[-_OVERLAP:]


def redact(text: str, secret_values: Iterable[str] = ()) -> tuple[str, int]:
    """``text`` with every secret value and credential shape replaced; and how many were."""
    count = 0
    for value in sorted({v for v in secret_values if len(v) >= _MIN_SECRET}, key=len, reverse=True):
        hits = text.count(value)
        if hits:
            text = text.replace(value, REDACTED)
            count += hits
    for pattern in _TEXT_PATTERNS:
        text, hits = pattern.subn(REDACTED, text)
        count += hits
    return text, count
