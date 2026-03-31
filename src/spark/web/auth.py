"""One-time authentication code manager for local access."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class _CodeRecord:
    code_hash: str
    created_at: datetime
    use_count: int = 0
    last_used: datetime | None = None


class AuthManager:
    """Manages single-use authentication codes for local browser sessions."""

    def __init__(self) -> None:
        self._codes: dict[str, _CodeRecord] = {}

    def generate_code(self) -> str:
        """Generate a new 8-character alphanumeric auth code."""
        code = secrets.token_hex(4).upper()
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        self._codes[code_hash] = _CodeRecord(
            code_hash=code_hash,
            created_at=datetime.now(timezone.utc),
        )
        return code

    def validate(self, code: str) -> bool:
        """Validate an auth code. Returns True if valid."""
        code_hash = hashlib.sha256(code.upper().encode()).hexdigest()
        record = self._codes.get(code_hash)
        if record is None:
            return False
        record.use_count += 1
        record.last_used = datetime.now(timezone.utc)
        return True
