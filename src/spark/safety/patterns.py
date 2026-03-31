"""Attack pattern definitions for prompt inspection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# -- Pattern collections -------------------------------------------------------

INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)ignore\s+(all\s+)?previous\s+(instructions|rules|prompts)", "high"),
    (r"(?i)disregard\s+(all\s+)?(prior|above|previous)", "high"),
    (r"(?i)forget\s+(everything|all)\s+(you|that)", "high"),
    (r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:new|different)", "medium"),
    (r"(?i)override\s+(?:your\s+)?(?:system|safety|instructions)", "high"),
    (r"(?i)\[system\]|\[INST\]|<\|system\|>|<\|im_start\|>system", "high"),
    (r"(?i)act\s+as\s+(?:if|though)\s+you\s+(?:have|had)\s+no\s+(?:rules|restrictions)", "high"),
]

JAILBREAK_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)DAN\s+mode|do\s+anything\s+now", "high"),
    (r"(?i)pretend\s+(?:you\s+)?(?:have|had)\s+no\s+(?:restrictions|limitations|rules)", "high"),
    (r"(?i)(?:enable|activate|enter)\s+(?:developer|god|admin|root)\s+mode", "high"),
    (r"(?i)you\s+(?:can|must|should)\s+(?:say|do|generate)\s+anything", "medium"),
    (
        r"(?i)(?:bypass|circumvent|disable)\s+(?:your\s+)?(?:safety|content|ethical)\s+(?:filters|guidelines)",
        "high",
    ),
]

CODE_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(?:exec|eval|system|os\.popen|subprocess)\s*\(", "high"),
    (r"(?i);\s*(?:rm\s+-rf|drop\s+table|delete\s+from|truncate)", "high"),
    (r"(?i)<script[^>]*>", "high"),
    (r"(?i)(?:'\s*OR\s*'1'\s*=\s*'1|--\s*$|;\s*--)", "medium"),
    (r"\$\(.*\)|`[^`]*`", "medium"),
]

PII_PATTERNS: list[tuple[str, str]] = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "medium"),  # SSN
    (r"\b(?:\d{4}[-\s]?){3}\d{4}\b", "medium"),  # Credit card
    (r"(?i)(?:password|api[_-]?key|secret[_-]?key|token)\s*[:=]\s*\S+", "low"),
]


@dataclass
class PatternMatch:
    """A single pattern match result."""

    category: str
    severity: str
    pattern: str
    matched_text: str


class PatternMatcher:
    """Fast regex-based pattern matching for prompt inspection."""

    def __init__(self) -> None:
        self._compiled: list[tuple[re.Pattern, str, str]] = []
        self._build_patterns()

    def _build_patterns(self) -> None:
        categories = [
            ("injection", INJECTION_PATTERNS),
            ("jailbreak", JAILBREAK_PATTERNS),
            ("code_injection", CODE_INJECTION_PATTERNS),
            ("pii", PII_PATTERNS),
        ]
        for category, patterns in categories:
            for pattern_str, severity in patterns:
                try:
                    compiled = re.compile(pattern_str)
                    self._compiled.append((compiled, category, severity))
                except re.error:
                    pass

    def scan(self, text: str) -> list[PatternMatch]:
        """Scan text for all matching patterns."""
        matches: list[PatternMatch] = []
        for compiled, category, severity in self._compiled:
            for m in compiled.finditer(text):
                matches.append(
                    PatternMatch(
                        category=category,
                        severity=severity,
                        pattern=compiled.pattern,
                        matched_text=m.group()[:100],
                    )
                )
        return matches

    def has_threats(self, text: str) -> bool:
        """Quick check: does the text contain any threat patterns?"""
        for compiled, _, _ in self._compiled:
            if compiled.search(text):
                return True
        return False

    def get_max_severity(self, matches: list[PatternMatch]) -> str:
        """Return the highest severity from a list of matches."""
        severity_order = {"high": 3, "medium": 2, "low": 1}
        if not matches:
            return "none"
        return max(matches, key=lambda m: severity_order.get(m.severity, 0)).severity
