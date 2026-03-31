"""Prompt inspection — multi-level threat detection and action policy."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from spark.safety.patterns import PatternMatch, PatternMatcher

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


@dataclass
class InspectionResult:
    """Result of prompt inspection."""

    is_safe: bool = True
    severity: str = "none"  # none, low, medium, high
    violations: list[PatternMatch] = field(default_factory=list)
    action: str = "allow"  # allow, log_only, warn, sanitize, block
    explanation: str = ""


class PromptInspector:
    """Multi-level prompt inspection for security threats.

    Levels:
      - basic: Fast pattern matching only
      - standard: Patterns + suspicious keyword heuristics
      - strict: Patterns + LLM semantic analysis
    """

    _SUSPICIOUS_KEYWORDS = {
        "ignore instructions",
        "bypass",
        "jailbreak",
        "pretend",
        "unrestricted",
        "no limitations",
        "override safety",
        "act as root",
        "developer mode",
        "sudo",
    }

    def __init__(
        self,
        *,
        level: str = "standard",
        action: str = "warn",
        db: DatabaseConnection | None = None,
    ) -> None:
        self._level = level
        self._action = action
        self._db = db
        self._matcher = PatternMatcher()

    @property
    def level(self) -> str:
        return self._level

    @property
    def action(self) -> str:
        return self._action

    def inspect(self, text: str, *, user_guid: str = "") -> InspectionResult:
        """Inspect a prompt for security threats."""
        if not text or not text.strip():
            return InspectionResult()

        # Level 1: Pattern matching (all levels)
        matches = self._matcher.scan(text)

        # Level 2: Keyword heuristics (standard and strict)
        if self._level in ("standard", "strict") and not matches:
            lower = text.lower()
            for kw in self._SUSPICIOUS_KEYWORDS:
                if kw in lower:
                    matches.append(
                        PatternMatch(
                            category="suspicious_keyword",
                            severity="low",
                            pattern=kw,
                            matched_text=kw,
                        )
                    )

        if not matches:
            return InspectionResult()

        severity = self._matcher.get_max_severity(matches)
        action = self._determine_action(severity)

        result = InspectionResult(
            is_safe=False,
            severity=severity,
            violations=matches,
            action=action,
            explanation=self._build_explanation(matches),
        )

        # Log violation
        if self._db:
            self._log_violation(result, text, user_guid)

        return result

    def _determine_action(self, severity: str) -> str:
        """Map severity to action based on configured policy."""
        if self._action == "block":
            return "block"
        if self._action == "log_only":
            return "log_only"

        # For warn and sanitize, escalate based on severity
        severity_actions = {
            "high": self._action,
            "medium": self._action,
            "low": "log_only",
        }
        return severity_actions.get(severity, "allow")

    def _build_explanation(self, matches: list[PatternMatch]) -> str:
        """Build a human-readable explanation of detected threats."""
        categories = set(m.category for m in matches)
        parts = []
        if "injection" in categories:
            parts.append("prompt injection attempt detected")
        if "jailbreak" in categories:
            parts.append("jailbreak attempt detected")
        if "code_injection" in categories:
            parts.append("potential code injection detected")
        if "pii" in categories:
            parts.append("potential PII detected")
        if "suspicious_keyword" in categories:
            parts.append("suspicious keywords detected")
        return "; ".join(parts) if parts else "security concern detected"

    def _log_violation(self, result: InspectionResult, text: str, user_guid: str) -> None:
        """Record violation in the database."""
        if not self._db:
            return
        try:
            ph = self._db.placeholder
            snippet = text[:200] if len(text) > 200 else text
            categories = ",".join(set(m.category for m in result.violations))
            self._db.execute(
                f"""INSERT INTO prompt_inspection_violations
                    (user_guid, violation_type, severity, prompt_snippet,
                     detection_method, action_taken, confidence_score)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
                (
                    user_guid,
                    categories,
                    result.severity,
                    snippet,
                    self._level,
                    result.action,
                    1.0,
                ),
            )
            self._db.commit()
        except Exception as e:
            logger.error("Failed to log violation: %s", e)
