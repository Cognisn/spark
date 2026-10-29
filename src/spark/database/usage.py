"""Usage tracking for token management and billing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from spark.db.engine import Executor, Row

__all__ = ["cleanup_old_usage", "get_token_usage_in_window", "get_usage_summary", "record_usage"]


async def record_usage(
    db: Executor,
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
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO usage_tracking
            (conversation_id, model_id, region, input_tokens, output_tokens, cost,
             timestamp, user_guid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (conversation_id, model_id, region, input_tokens, output_tokens, cost, now, user_guid),
    )


async def get_token_usage_in_window(db: Executor, user_guid: str, since: str) -> dict[str, Any]:
    """Get total input/output tokens since a timestamp."""
    row = await db.fetch_one(
        """SELECT COALESCE(SUM(input_tokens), 0) as input_tokens,
                  COALESCE(SUM(output_tokens), 0) as output_tokens,
                  COALESCE(SUM(cost), 0.0) as total_cost
           FROM usage_tracking WHERE user_guid = ? AND timestamp >= ?""",
        (user_guid, since),
    )
    if row is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_cost": 0.0}
    return {
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "total_cost": float(row["total_cost"]),
    }


async def get_usage_summary(db: Executor, user_guid: str, since: str) -> list[Row]:
    """Get usage breakdown by model and region."""
    rows = await db.fetch_all(
        """SELECT model_id, region,
                  SUM(input_tokens) as input_tokens,
                  SUM(output_tokens) as output_tokens,
                  SUM(cost) as total_cost,
                  COUNT(*) as request_count
           FROM usage_tracking WHERE user_guid = ? AND timestamp >= ?
           GROUP BY model_id, region
           ORDER BY total_cost DESC""",
        (user_guid, since),
    )
    for row in rows:
        row["input_tokens"] = int(row["input_tokens"])
        row["output_tokens"] = int(row["output_tokens"])
        row["total_cost"] = float(row["total_cost"])
        row["request_count"] = int(row["request_count"])
    return rows


async def cleanup_old_usage(db: Executor, before: str) -> int:
    """Delete usage records before a cutoff timestamp. Returns count deleted."""
    return await db.execute("DELETE FROM usage_tracking WHERE timestamp < ?", (before,))
