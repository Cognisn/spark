"""Per-agent visible-history builders.

Isolation is achieved here by construction: builders receive only structured
turns and exhibits, never briefs and never tool activity, so leakage is
structurally impossible rather than procedurally prevented.
"""

from __future__ import annotations

from typing import Any

DEBATER_VISIBLE_TYPES: frozenset[str] = frozenset(
    {"announcement", "interim", "argument", "user_prompt"}
)
JUDGE_VISIBLE_TYPES: frozenset[str] = DEBATER_VISIBLE_TYPES | frozenset(
    {"ruling", "qa_question", "qa_answer"}
)

_HEADINGS = {
    "announcement": "JUDGE (announcement)",
    "interim": "JUDGE (interim remarks)",
    "ruling": "JUDGE (final ruling)",
    "user_prompt": "USER DIRECTIVE",
    "qa_question": "USER QUESTION",
    "qa_answer": "JUDGE ANSWER",
}


def _render_exhibits(items: list[dict[str, Any]]) -> str:
    lines = []
    for ex in items:
        src = f" (source: {ex['source']})" if ex.get("source") else ""
        lines.append(f"Exhibit {ex['label']}: {ex.get('title', '')}{src}\n{ex.get('content', '')}")
    return "\n".join(lines)


def _render_turn(
    turn: dict[str, Any],
    exhibits: dict[int, list[dict[str, Any]]],
    summarise_before_round: int,
) -> str:
    body = turn.get("content") or ""
    if (
        turn.get("summary")
        and turn["turn_type"] == "argument"
        and turn["round"] < summarise_before_round
    ):
        body = f"[Summarised] {turn['summary']}"
    if turn["turn_type"] == "argument":
        heading = f"{turn['role'].upper()} ARGUMENT (round {turn['round']})"
        ex = exhibits.get(turn["id"], [])
        if ex and turn["round"] >= summarise_before_round:
            body += "\n\n" + _render_exhibits(ex)
    else:
        heading = _HEADINGS.get(turn["turn_type"], turn["turn_type"].upper())
    return f"### {heading}\n{body}"


def _build(
    turns: list[dict[str, Any]],
    exhibits: dict[int, list[dict[str, Any]]],
    visible: frozenset[str],
    summarise_before_round: int,
) -> str:
    parts = [
        _render_turn(t, exhibits, summarise_before_round)
        for t in turns
        if t["turn_type"] in visible and t.get("status") == "complete"
    ]
    return "\n\n".join(parts) if parts else "(no debate activity yet)"


def build_debater_transcript(
    role: str,
    turns: list[dict[str, Any]],
    exhibits: dict[int, list[dict[str, Any]]],
    *,
    summarise_before_round: int = 0,
) -> str:
    """Everything a debater may see: announcements, interim remarks, both
    sides' submitted arguments with exhibits, and user directives."""
    return _build(turns, exhibits, DEBATER_VISIBLE_TYPES, summarise_before_round)


def build_judge_transcript(
    turns: list[dict[str, Any]],
    exhibits: dict[int, list[dict[str, Any]]],
    *,
    include_qa: bool = False,
    summarise_before_round: int = 0,
) -> str:
    """Everything the judge may see. Never includes briefs or tool activity."""
    visible = (
        JUDGE_VISIBLE_TYPES
        if include_qa
        else (JUDGE_VISIBLE_TYPES - frozenset({"qa_question", "qa_answer"}))
    )
    return _build(turns, exhibits, visible, summarise_before_round)
