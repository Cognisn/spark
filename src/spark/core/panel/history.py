"""Panel transcript builder.

Panels have shared visibility: every panellist sees every completed
contribution. Isolation still holds by construction for what panellists must
not see — briefs, tool activity, and the moderator's synthesis and QA are
never passed in or are filtered by turn type here.
"""

from __future__ import annotations

from typing import Any

from spark.core.debate.history import _render_exhibits

PANELLIST_VISIBLE_TYPES: frozenset[str] = frozenset(
    {"announcement", "contribution", "interim", "user_prompt"}
)
MODERATOR_VISIBLE_TYPES: frozenset[str] = PANELLIST_VISIBLE_TYPES | frozenset(
    {"synthesis", "qa_question", "qa_answer"}
)

_HEADINGS = {
    "announcement": "MODERATOR (framing)",
    "interim": "MODERATOR (interim remarks)",
    "synthesis": "MODERATOR (synthesis)",
    "user_prompt": "USER DIRECTIVE",
    "qa_question": "USER QUESTION",
    "qa_answer": "MODERATOR ANSWER",
}


def _speaker_name(role: str, agents: dict[str, dict[str, Any]]) -> str:
    agent = agents.get(role) or {}
    return agent.get("display_name") or role


def _render_turn(
    turn: dict[str, Any],
    exhibits: dict[int, list[dict[str, Any]]],
    agents: dict[str, dict[str, Any]],
) -> str:
    body = turn.get("content") or ""
    if turn["turn_type"] == "contribution":
        name = _speaker_name(turn["role"], agents)
        heading = f"{name} (round {turn['round']})"
        ex = exhibits.get(turn["id"], [])
        if ex:
            body += "\n\n" + _render_exhibits(ex)
    else:
        heading = _HEADINGS.get(turn["turn_type"], turn["turn_type"].upper())
    return f"### {heading}\n{body}"


def build_panel_transcript(
    turns: list[dict[str, Any]],
    exhibits: dict[int, list[dict[str, Any]]],
    agents: dict[str, dict[str, Any]],
    *,
    for_moderator: bool = False,
    include_qa: bool = False,
) -> str:
    """The shared discussion transcript, filtered by audience.

    Panellists see framing, contributions with exhibits, interim remarks, and
    user directives. The moderator additionally sees the synthesis, and QA
    exchanges when include_qa is set.
    """
    visible = MODERATOR_VISIBLE_TYPES if for_moderator else PANELLIST_VISIBLE_TYPES
    if for_moderator and not include_qa:
        visible = visible - frozenset({"qa_question", "qa_answer"})
    parts = [
        _render_turn(t, exhibits, agents)
        for t in turns
        if t["turn_type"] in visible and t.get("status") == "complete"
    ]
    return "\n\n".join(parts) if parts else "(no panel activity yet)"
