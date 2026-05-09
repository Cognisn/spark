"""Cooperative cancellation token used by the agent executor and conversation manager."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class CancellationToken:
    """Thread-safe flag for cooperative cancellation.

    Holders check `is_cancelled()` at safe points (between LLM iterations).
    The first call to `cancel()` records the reason; subsequent calls are no-ops.
    """

    _event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""

    def cancel(self, reason: str = "") -> None:
        if self._event.is_set():
            return
        self.reason = reason
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()
