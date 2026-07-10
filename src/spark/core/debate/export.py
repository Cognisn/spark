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


def _markdown_to_html(text: str) -> str:
    """Minimal markdown-to-HTML over ALREADY-ESCAPED text (no raw HTML passthrough)."""
    import re

    lines = (text or "").split("\n")
    out: list[str] = []
    in_list = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            out.append("<p>" + " ".join(paragraph) + "</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def inline(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif stripped.startswith("- "):
            flush_paragraph()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
        elif not stripped:
            flush_paragraph()
            close_list()
        else:
            close_list()
            paragraph.append(inline(stripped))
    flush_paragraph()
    close_list()
    return "\n".join(out)


def export_debate_html(db: Any, conversation_id: int) -> str:
    """A fully self-contained HTML document of the debate transcript."""
    import html as html_mod

    cfg, turns, exhibits = _load(db, conversation_id)

    def esc(value: Any) -> str:
        return html_mod.escape(str(value or ""))

    def md(value: Any) -> str:
        return _markdown_to_html(esc(value))

    sections: list[str] = []
    sections.append(f"<h1>Debate: {esc(cfg['topic'])}</h1>")
    mode = esc(cfg["rounds_mode"]) + (
        f", max {esc(cfg['max_rounds'])} rounds" if cfg["max_rounds"] else ""
    )
    sections.append(f'<p class="meta">Mode: {mode}</p>')

    current_round = None
    for turn in turns:
        if turn["status"] != "complete":
            continue
        if turn["turn_type"] == "argument" and turn["round"] != current_round:
            current_round = turn["round"]
            sections.append(f"<h2 class='round'>Round {esc(current_round)}</h2>")
        if turn["turn_type"] == "argument":
            side = "pro" if turn["role"] == "pro" else "con"
            label = "Pro" if side == "pro" else "Against"
            sections.append(f'<div class="argument {side}"><h3>{label}</h3>')
            sections.append(md(turn.get("content")))
            for ex in exhibits.get(turn["id"], []):
                src = f' <span class="source">({esc(ex.get("source"))})</span>' if ex.get("source") else ""
                sections.append(
                    f'<div class="exhibit"><strong>Exhibit {esc(ex.get("label"))}: '
                    f"{esc(ex.get('title'))}</strong>{src}<div>{esc(ex.get('content'))}</div></div>"
                )
            sections.append("</div>")
        elif turn["turn_type"] == "ruling":
            sections.append(
                '<div class="judgement"><h2>Final Judgement</h2>'
                + md(turn.get("content"))
                + "</div>"
            )
        elif turn["turn_type"] in ("announcement", "interim"):
            title = "Judge, opening" if turn["turn_type"] == "announcement" else "Judge, interim remarks"
            sections.append(f'<div class="judge-note"><h3>{title}</h3>{md(turn.get("content"))}</div>')
        elif turn["turn_type"] == "user_prompt":
            sections.append(f'<div class="user-note"><h3>User directive</h3>{md(turn.get("content"))}</div>')
        elif turn["turn_type"] == "qa_question":
            sections.append(f'<div class="user-note"><h3>User question</h3>{md(turn.get("content"))}</div>')
        elif turn["turn_type"] == "qa_answer":
            sections.append(f'<div class="judge-note"><h3>Judge answer</h3>{md(turn.get("content"))}</div>')

    style = (
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "max-width:860px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1c2430;}"
        "h1{border-bottom:2px solid #2a6df4;padding-bottom:0.4rem;}"
        ".meta{color:#5b6675;}"
        ".round{margin-top:2rem;border-bottom:1px solid #d5dbe3;}"
        ".argument{border:1px solid #d5dbe3;border-radius:8px;padding:0.75rem 1rem;margin:0.75rem 0;}"
        ".argument.pro h3{color:#1f7a4d;}.argument.con h3{color:#a03030;}"
        ".exhibit{border:1px solid #cfd6df;border-left:4px solid #2a6df4;border-radius:6px;"
        "padding:0.5rem 0.75rem;margin:0.5rem 0;background:#f5f7fa;}"
        ".exhibit .source{color:#5b6675;font-size:0.85em;}"
        ".judgement{border:2px solid #2a6df4;border-radius:8px;padding:1rem;margin:1.5rem 0;"
        "background:#eef3fe;}"
        ".judge-note,.user-note{margin:0.75rem 0;padding:0.5rem 0.75rem;border-left:3px solid #98a3b3;}"
        "code{background:#eef1f5;padding:0 0.25rem;border-radius:3px;}"
    )
    return (
        "<!DOCTYPE html>\n<html lang='en'>\n<head>\n<meta charset='utf-8'>\n"
        f"<title>Debate: {esc(cfg['topic'])}</title>\n<style>{style}</style>\n</head>\n<body>\n"
        + "\n".join(sections)
        + "\n</body>\n</html>\n"
    )
