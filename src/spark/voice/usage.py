"""The monthly character cap: a local guard, independent of the ElevenLabs plan."""

from __future__ import annotations

from spark.database import voice_usage
from spark.db.engine import Executor


async def over_cap(db: Executor, user_guid: str, cap: int) -> bool:
    """True when the user has reached their configured cap. 0 means unlimited."""
    if not cap or cap <= 0:
        return False
    return await voice_usage.characters_this_month(db, user_guid) >= cap
