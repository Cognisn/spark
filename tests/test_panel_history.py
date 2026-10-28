"""Shared-visibility transcript for panels."""

from __future__ import annotations

from spark.core.panel.history import build_panel_transcript

AGENTS = {
    "moderator": {"display_name": "Moderator", "is_human": False},
    "panellist:1": {"display_name": "Economist", "is_human": False},
    "panellist:2": {"display_name": "Matthew", "is_human": True},
}

TURNS = [
    {
        "id": 1,
        "round": 0,
        "role": "moderator",
        "turn_type": "announcement",
        "content": "Welcome.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 2,
        "round": 1,
        "role": "panellist:1",
        "turn_type": "contribution",
        "content": "Growth matters.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 3,
        "round": 1,
        "role": "panellist:2",
        "turn_type": "contribution",
        "content": "Lived experience says otherwise.",
        "summary": None,
        "status": "complete",
    },
    {
        "id": 4,
        "round": 1,
        "role": "panellist:1",
        "turn_type": "contribution",
        "content": "FAILED",
        "summary": None,
        "status": "failed",
    },
    {
        "id": 5,
        "round": 0,
        "role": "moderator",
        "turn_type": "synthesis",
        "content": "In summary.",
        "summary": None,
        "status": "complete",
    },
]
EXHIBITS = {2: [{"label": "A", "title": "Data", "content": "Numbers", "source": None}]}


def test_panellists_see_all_contributions_with_names() -> None:
    t = build_panel_transcript(TURNS, EXHIBITS, AGENTS)
    assert "Economist (round 1)" in t
    assert "Matthew (round 1)" in t
    assert "Growth matters." in t
    assert "Lived experience" in t
    assert "Exhibit A" in t


def test_failed_and_synthesis_hidden_from_panellists() -> None:
    t = build_panel_transcript(TURNS, EXHIBITS, AGENTS)
    assert "FAILED" not in t
    assert "In summary." not in t


def test_moderator_sees_synthesis() -> None:
    t = build_panel_transcript(TURNS, EXHIBITS, AGENTS, for_moderator=True)
    assert "In summary." in t


def test_empty_transcript_placeholder() -> None:
    assert "(no panel activity yet)" in build_panel_transcript([], {}, AGENTS)
