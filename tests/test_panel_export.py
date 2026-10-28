"""Panel-aware transcript export."""

from __future__ import annotations

import pytest

from spark.core.debate.export import export_debate_html, export_debate_markdown
from spark.database import debates


@pytest.fixture()
def panel_cid(sync_db) -> int:
    cid = sync_db.fetch_value(
        "INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        "VALUES (?, ?, ?, 'panel') RETURNING id",
        ("P", "m", "u1"),
    )
    agents = {
        "moderator": {"model_id": "m", "brief": None, "display_name": "Moderator"},
        "panellist:1": {"model_id": "a", "brief": None, "display_name": "Economist"},
        "panellist:2": {
            "model_id": "",
            "brief": None,
            "display_name": "Matthew",
            "is_human": True,
        },
    }
    sync_db.call(debates.create_debate, cid, "Topic T", "fixed", 1, "u1", agents)
    sync_db.call(debates.add_turn, cid, 0, "moderator", "announcement", "Welcome & frame.")
    t1 = sync_db.call(
        debates.add_turn, cid, 1, "panellist:1", "contribution", "Growth **matters**."
    )
    sync_db.call(
        debates.add_exhibits,
        t1,
        [{"label": "A", "title": "Data", "content": "Numbers", "source": "ABS"}],
    )
    sync_db.call(debates.add_turn, cid, 1, "panellist:2", "contribution", "Lived experience.")
    sync_db.call(debates.add_turn, cid, 0, "moderator", "synthesis", "Broad agreement overall.")
    return cid


def test_markdown_uses_names_and_synthesis(sync_db, panel_cid) -> None:
    md = export_debate_markdown(sync_db, panel_cid)
    assert "# Panel: Topic T" in md
    assert "### Economist" in md
    assert "### Matthew" in md
    assert "## Synthesis" in md
    assert "Broad agreement overall." in md
    assert "Exhibit A" in md


def test_html_uses_names_and_synthesis(sync_db, panel_cid) -> None:
    html = export_debate_html(sync_db, panel_cid)
    assert "Panel: Topic T" in html
    assert "<h3>Economist</h3>" in html
    assert "<h2>Synthesis</h2>" in html
    assert "Welcome &amp; frame." in html
    assert "<strong>matters</strong>" in html
