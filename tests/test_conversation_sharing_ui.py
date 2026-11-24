"""The participant colours and the sharing pieces of the pages.

Each of the six participant colours is read from spark.css for both themes and must
reach 4.5:1 (WCAG AA for text) against every background a name sits on in that theme;
the six must also stay apart from one another, so two people never look alike. The
chat page and the list carry the sharing pieces the scripts drive.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import spark

WEB = Path(spark.__file__).parent / "web"
CSS = (WEB / "static" / "css" / "spark.css").read_text(encoding="utf-8")

# The backgrounds a participant's name sits on: page, cards, message bubbles, panels.
BACKGROUNDS = {
    "light": ["#ffffff", "#f0f4fc", "#f4f7fd", "#e8eef8", "#f9fbfe"],
    "dark": ["#080e1e", "#0c1428", "#111c35", "#162240", "#0b1327"],
}


def _palette(theme: str) -> dict[int, str]:
    block = re.search(r'\[data-bs-theme="' + theme + r'"\]\s*\{([^}]*--participant-1[^}]*)\}', CSS)
    assert block, theme
    return {
        int(n): value.lower()
        for n, value in re.findall(r"--participant-(\d):\s*(#[0-9a-fA-F]{6})", block.group(1))
    }


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_six_colours_each_theme(theme: str) -> None:
    assert sorted(_palette(theme)) == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_colour_reads_on_every_background(theme: str) -> None:
    for n, colour in _palette(theme).items():
        for background in BACKGROUNDS[theme]:
            ratio = contrast(colour, background)
            assert ratio >= 4.5, f"{theme} colour {n} {colour} on {background}: {ratio:.2f}"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_colours_differ_from_one_another(theme: str) -> None:
    colours = list(_palette(theme).values())
    assert len(set(colours)) == 6
    for i, a in enumerate(colours):
        for b in colours[i + 1 :]:
            distance = sum(abs(int(a[k : k + 2], 16) - int(b[k : k + 2], 16)) for k in (1, 3, 5))
            assert distance >= 60, (a, b)


def test_every_colour_has_its_class() -> None:
    for n in range(1, 7):
        assert f".participant-{n} {{ --participant: var(--participant-{n}); }}" in CSS


def test_the_pages_carry_the_sharing_pieces() -> None:
    chat = (WEB / "templates" / "chat.html").read_text(encoding="utf-8")
    dialog = (WEB / "templates" / "_share_dialog.html").read_text(encoding="utf-8")
    listing = (WEB / "templates" / "conversations.html").read_text(encoding="utf-8")
    assert 'id="btn-share"' in chat and "_share_dialog.html" in chat
    assert 'id="shared-indicator"' in chat and 'id="turn-waiting"' in chat
    assert "/static/js/share.js" in chat
    assert "Everything already in this conversation becomes visible" in dialog
    assert 'id="share-search"' in dialog and 'id="share-members"' in dialog
    assert "Shared with me" in listing and "/conversations/api/shared" in listing
    script = (WEB / "static" / "js" / "share.js").read_text(encoding="utf-8")
    assert "/stream/conversation?conversation_id=" in script
    assert "Waiting for ${state.turn.name}'s turn to finish" in script
