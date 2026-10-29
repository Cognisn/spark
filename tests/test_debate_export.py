"""Tests for the debate exporter."""

from __future__ import annotations

import json

from spark.core.debate.export import export_debate_json, export_debate_markdown
from spark.database import debates

AGENTS = {
    "pro": {"model_id": "model-a", "brief": None},
    "con": {"model_id": "model-b", "brief": None},
    "judge": {"model_id": "model-c", "brief": None},
}


def _setup_debate(sync_db) -> int:
    cid = sync_db.fetch_value(
        "INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        "VALUES (?, ?, ?, 'debate') RETURNING id",
        ("D", "model-c", "u1"),
    )
    sync_db.call(debates.create_debate, cid, "Topic T", "fixed", 1, "u1", AGENTS)
    return cid


def test_markdown_export_structure(sync_db) -> None:
    cid = _setup_debate(sync_db)
    tid = sync_db.call(debates.add_turn, cid, 1, "pro", "argument", "Pro case.")
    sync_db.call(
        debates.add_exhibits, tid, [{"label": "A", "title": "T", "content": "C", "source": "s"}]
    )
    sync_db.call(debates.add_turn, cid, 0, "judge", "ruling", "Pro wins.")

    md = export_debate_markdown(sync_db, cid)
    assert "# Debate: Topic T" in md
    assert "## Round 1" in md and "Pro case." in md
    assert "Exhibit A" in md and "## Ruling" in md and "Pro wins." in md


def test_failed_turns_excluded(sync_db) -> None:
    cid = _setup_debate(sync_db)
    sync_db.call(debates.add_turn, cid, 1, "pro", "argument", "FAILED DRAFT", status="failed")
    md = export_debate_markdown(sync_db, cid)
    assert "FAILED DRAFT" not in md


def test_json_export_round_trips(sync_db) -> None:
    cid = _setup_debate(sync_db)
    sync_db.call(debates.add_turn, cid, 1, "pro", "argument", "Pro case.")
    data = json.loads(export_debate_json(sync_db, cid))
    assert data["topic"] == "Topic T"
    assert data["turns"][0]["content"] == "Pro case."


def test_html_export_structure_and_escaping(sync_db) -> None:
    from spark.core.debate.export import export_debate_html

    cid = _setup_debate(sync_db)
    tid = sync_db.call(
        debates.add_turn,
        cid,
        1,
        "pro",
        "argument",
        "## Case\n**Point** <script>alert(1)</script>",
    )
    sync_db.call(
        debates.add_exhibits,
        tid,
        [{"label": "A", "title": "T<b>", "content": "C & D", "source": "s"}],
    )
    sync_db.call(debates.add_turn, cid, 0, "judge", "ruling", "Pro wins.")

    html_doc = export_debate_html(sync_db, cid)
    assert html_doc.startswith("<!DOCTYPE html>")
    assert "<script>alert(1)</script>" not in html_doc  # escaped
    assert "&lt;script&gt;" in html_doc
    assert "<h2>Case</h2>" in html_doc  # markdown converted
    assert "<strong>Point</strong>" in html_doc
    assert "Final Judgement" in html_doc and "Pro wins." in html_doc
    assert "Exhibit A" in html_doc and "C &amp; D" in html_doc
    assert "http" not in html_doc.split("</style>")[0]  # no external resources
