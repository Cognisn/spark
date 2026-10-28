"""Panel-aware transcript export."""

from __future__ import annotations

import pytest

from spark.core.debate.export import export_debate_html, export_debate_markdown
from spark.database import Database, debates
from spark.database.backends import SQLiteBackend
from spark.database.connection import DatabaseConnection


@pytest.fixture()
def db(tmp_path) -> DatabaseConnection:
    """Create a fresh test database and return its connection."""
    backend = SQLiteBackend(tmp_path / "test.db")
    conn = DatabaseConnection(backend)
    Database(conn)  # initialises the schema
    return conn


@pytest.fixture()
def panel_cid(db) -> int:
    ph = db.placeholder
    cur = db.execute(
        f"INSERT INTO conversations (name, model_id, user_guid, conversation_type) "
        f"VALUES ({ph}, {ph}, {ph}, 'panel')",
        ("P", "m", "u1"),
    )
    db.commit()
    cid = cur.lastrowid
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
    debates.create_debate(db, cid, "Topic T", "fixed", 1, "u1", agents)
    debates.add_turn(db, cid, 0, "moderator", "announcement", "Welcome & frame.")
    t1 = debates.add_turn(db, cid, 1, "panellist:1", "contribution", "Growth **matters**.")
    debates.add_exhibits(
        db, t1, [{"label": "A", "title": "Data", "content": "Numbers", "source": "ABS"}]
    )
    debates.add_turn(db, cid, 1, "panellist:2", "contribution", "Lived experience.")
    debates.add_turn(db, cid, 0, "moderator", "synthesis", "Broad agreement overall.")
    return cid


def test_markdown_uses_names_and_synthesis(db, panel_cid) -> None:
    md = export_debate_markdown(db, panel_cid)
    assert "# Panel: Topic T" in md
    assert "### Economist" in md
    assert "### Matthew" in md
    assert "## Synthesis" in md
    assert "Broad agreement overall." in md
    assert "Exhibit A" in md


def test_html_uses_names_and_synthesis(db, panel_cid) -> None:
    html = export_debate_html(db, panel_cid)
    assert "Panel: Topic T" in html
    assert "<h3>Economist</h3>" in html
    assert "<h2>Synthesis</h2>" in html
    assert "Welcome &amp; frame." in html
    assert "<strong>matters</strong>" in html
