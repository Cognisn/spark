"""Usage tracking for token management and billing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection


def record_usage(
    db: DatabaseConnection,
    conversation_id: int,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    user_guid: str,
    *,
    region: str | None = None,
    cost: float = 0.0,
) -> None:
    """Record a token usage event."""
    ph = db.placeholder
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        f"""INSERT INTO usage_tracking
            (conversation_id, model_id, region, input_tokens, output_tokens, cost, timestamp, user_guid)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
        (conversation_id, model_id, region, input_tokens, output_tokens, cost, now, user_guid),
    )
    db.commit()


def get_token_usage_in_window(db: DatabaseConnection, user_guid: str, since: str) -> dict:
    """Get total input/output tokens since a timestamp."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens,
                   COALESCE(SUM(cost), 0.0) as total_cost
            FROM usage_tracking WHERE user_guid = {ph} AND timestamp >= {ph}""",
        (user_guid, since),
    )
    row = cursor.fetchone()
    return dict(row) if row else {"input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}


def get_usage_summary(db: DatabaseConnection, user_guid: str, since: str) -> list[dict]:
    """Get usage breakdown by model and region."""
    ph = db.placeholder
    cursor = db.execute(
        f"""SELECT model_id, region,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cost) as total_cost,
                   COUNT(*) as request_count
            FROM usage_tracking WHERE user_guid = {ph} AND timestamp >= {ph}
            GROUP BY model_id, region
            ORDER BY total_cost DESC""",
        (user_guid, since),
    )
    return [dict(row) for row in cursor.fetchall()]


def cleanup_old_usage(db: DatabaseConnection, before: str) -> int:
    """Delete usage records before a cutoff timestamp. Returns count deleted."""
    ph = db.placeholder
    cursor = db.execute(
        f"DELETE FROM usage_tracking WHERE timestamp < {ph}",
        (before,),
    )
    db.commit()
    return cursor.rowcount
