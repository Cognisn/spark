"""Voice synthesis usage ledger: characters billed, per user and conversation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

# Portable "start of the current calendar month".
_MONTH_START = "strftime('%Y-%m-01', 'now')"


def record(
    db: DatabaseConnection,
    user_guid: str,
    characters: int,
    *,
    conversation_id: int | None = None,
    model_id: str | None = None,
) -> None:
    """Bill characters against a user (and optionally a conversation)."""
    ph = db.placeholder
    db.execute(
        f"""INSERT INTO voice_usage
            (conversation_id, user_guid, characters, model_id)
            VALUES ({ph}, {ph}, {ph}, {ph})""",
        (conversation_id, user_guid, int(characters), model_id),
    )
    db.commit()


def characters_this_month(db: DatabaseConnection, user_guid: str) -> int:
    """Characters billed to this user since the start of the calendar month."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT COALESCE(SUM(characters), 0) FROM voice_usage
            WHERE user_guid = {ph} AND created_at >= {_MONTH_START}""",
        (user_guid,),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def usage_summary(db: DatabaseConnection, user_guid: str) -> dict[str, Any]:
    """This month's total, plus a per-conversation breakdown."""
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT conversation_id, SUM(characters) FROM voice_usage
            WHERE user_guid = {ph} AND created_at >= {_MONTH_START}
              AND conversation_id IS NOT NULL
            GROUP BY conversation_id ORDER BY SUM(characters) DESC""",
        (user_guid,),
    )
    return {
        "characters_this_month": characters_this_month(db, user_guid),
        "by_conversation": [
            {"conversation_id": cid, "characters": int(total)}
            for cid, total in cur.fetchall()
        ],
    }
