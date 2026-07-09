"""Render a debate transcript for export, from debate_turns and exhibits."""

from __future__ import annotations

import json
from typing import Any

from spark.database import debates


def _load(db: Any, conversation_id: int) -> tuple[dict, list[dict], dict]:
    cfg = debates.get_debate(db, conversation_id)
    if not cfg:
        raise ValueError(f"No debate for conversation {conversation_id}")
    return (
        cfg,
        debates.get_turns(db, conversation_id),
        debates.get_exhibits(db, conversation_id),
    )


def export_debate_markdown(db: Any, conversation_id: int) -> str:
    cfg, turns, exhibits = _load(db, conversation_id)
    lines = [f"# Debate: {cfg['topic']}", ""]
    lines.append(
        f"Mode: {cfg['rounds_mode']}"
        + (f", max {cfg['max_rounds']} rounds" if cfg["max_rounds"] else "")
    )
    lines.append("")
    current_round = None
    for t in turns:
        if t["status"] != "complete":
            continue
        if t["turn_type"] == "argument" and t["round"] != current_round:
            current_round = t["round"]
            lines.append(f"## Round {current_round}")
            lines.append("")
        heading = {
            "announcement": "### Judge, opening",
            "interim": "### Judge, interim remarks",
            "argument": f"### {t['role'].upper()}",
            "user_prompt": "### User directive",
            "ruling": "## Ruling",
            "qa_question": "### User question",
            "qa_answer": "### Judge answer",
        }.get(t["turn_type"], f"### {t['turn_type']}")
        lines.append(heading)
        lines.append(t.get("content") or "")
        for ex in exhibits.get(t["id"], []):
            src = f" ({ex['source']})" if ex.get("source") else ""
            lines.append(
                f"\n> **Exhibit {ex['label']}: {ex.get('title', '')}**{src}\n> "
                + (ex.get("content") or "").replace("\n", "\n> ")
            )
        lines.append("")
    return "\n".join(lines)


def export_debate_json(db: Any, conversation_id: int) -> str:
    cfg, turns, exhibits = _load(db, conversation_id)
    return json.dumps(
        {
            "topic": cfg["topic"],
            "rounds_mode": cfg["rounds_mode"],
            "max_rounds": cfg["max_rounds"],
            "state": cfg["state"],
            "opening_speaker": cfg["opening_speaker"],
            "agents": cfg["agents"],
            "turns": [
                {**t, "exhibits": exhibits.get(t["id"], [])}
                for t in turns
                if t["status"] == "complete"
            ],
        },
        indent=2,
        default=str,
    )
