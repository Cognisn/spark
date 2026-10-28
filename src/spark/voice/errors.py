"""Voice failures and their classification.

Every failure downgrades an utterance to the browser synthesiser, so the
reason exists to explain the fallback to the user, never to abort speech.
"""

from __future__ import annotations

from typing import Any

_QUOTA_MARKER = "quota_exceeded"


class VoiceUnavailable(Exception):
    """Raised when ElevenLabs cannot serve an utterance. Callers fall back."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Voice unavailable: {reason}")
        self.reason = reason


def classify_error(status: int, body: Any) -> str:
    """Map an ElevenLabs HTTP failure to a fallback reason.

    Quota detection is deliberately defensive. The current docs return 402 for
    exhausted credits, but the older behaviour (a 401/422 carrying
    'quota_exceeded' in the detail) is still observed, so we inspect the body
    as well as the status code.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        marker = f"{detail.get('status', '')} {detail.get('code', '')}"
        if _QUOTA_MARKER in marker:
            return "quota"

    if status == 402:
        return "quota"
    if status in (401, 403):
        return "bad_key"
    if status == 429:
        return "rate_limited"
    if status in (400, 422):
        return "bad_request"
    return "server"
