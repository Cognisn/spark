"""Prompt inspection violations."""

from __future__ import annotations

from typing import Any

from spark.db.engine import Executor

__all__ = ["list_violations", "record_violation"]


async def record_violation(
    db: Executor,
    user_guid: str,
    *,
    violation_type: str,
    severity: str,
    prompt_snippet: str,
    detection_method: str,
    action_taken: str,
    confidence_score: float,
    conversation_id: int | None = None,
) -> None:
    """Record a prompt inspection violation (a user prompt or a tool result)."""
    await db.execute(
        """INSERT INTO prompt_inspection_violations
            (user_guid, conversation_id, violation_type, severity, prompt_snippet,
             detection_method, action_taken, confidence_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_guid,
            conversation_id,
            violation_type,
            severity,
            prompt_snippet,
            detection_method,
            action_taken,
            confidence_score,
        ),
    )


async def list_violations(
    db: Executor, user_guid: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """A user's most recent violations, newest first."""
    return await db.fetch_all(
        """SELECT id, user_guid, conversation_id, violation_type, severity, prompt_snippet,
                  detection_method, action_taken, confidence_score, timestamp
           FROM prompt_inspection_violations
           WHERE user_guid = ?
           ORDER BY id DESC
           LIMIT ?""",
        (user_guid, limit),
    )
