"""The monthly character cap — a local guard, independent of the ElevenLabs plan."""

from __future__ import annotations

from typing import Any

from spark.database import voice_usage


def over_cap(db: Any, user_guid: str, cap: int) -> bool:
    """True when the user has reached their configured cap. 0 means unlimited."""
    if not cap or cap <= 0:
        return False
    return voice_usage.characters_this_month(db, user_guid) >= cap
