"""Prompt inspection violations."""

from __future__ import annotations

from spark.db.engine import Executor

__all__ = ["record_violation"]


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
) -> None:
    """Record a prompt inspection violation."""
    await db.execute(
        """INSERT INTO prompt_inspection_violations
            (user_guid, violation_type, severity, prompt_snippet,
             detection_method, action_taken, confidence_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_guid,
            violation_type,
            severity,
            prompt_snippet,
            detection_method,
            action_taken,
            confidence_score,
        ),
    )
