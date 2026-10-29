"""Voice synthesis usage ledger: characters billed, per user and conversation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.db.engine import Executor

__all__ = ["characters_this_month", "record", "usage_summary"]


def _month_start() -> str:
    """Start of the current calendar month (UTC), as both engines compare it."""
    return datetime.now(timezone.utc).strftime("%Y-%m-01 00:00:00")


async def record(
    db: Executor,
    user_guid: str,
    characters: int,
    *,
    conversation_id: int | None = None,
    model_id: str | None = None,
) -> None:
    """Bill characters against a user (and optionally a conversation)."""
    await db.execute(
        """INSERT INTO voice_usage
            (conversation_id, user_guid, characters, model_id)
            VALUES (?, ?, ?, ?)""",
        (conversation_id, user_guid, int(characters), model_id),
    )


async def characters_this_month(db: Executor, user_guid: str) -> int:
    """Characters billed to this user since the start of the calendar month."""
    total = await db.fetch_value(
        """SELECT COALESCE(SUM(characters), 0) FROM voice_usage
            WHERE user_guid = ? AND created_at >= ?""",
        (user_guid, _month_start()),
    )
    return int(total) if total is not None else 0


async def usage_summary(db: Executor, user_guid: str) -> dict[str, Any]:
    """This month's total, plus a per-conversation breakdown."""
    rows = await db.fetch_all(
        """SELECT conversation_id, SUM(characters) AS total FROM voice_usage
            WHERE user_guid = ? AND created_at >= ?
              AND conversation_id IS NOT NULL
            GROUP BY conversation_id ORDER BY SUM(characters) DESC""",
        (user_guid, _month_start()),
    )
    return {
        "characters_this_month": await characters_this_month(db, user_guid),
        "by_conversation": [
            {"conversation_id": r["conversation_id"], "characters": int(r["total"])} for r in rows
        ],
    }
