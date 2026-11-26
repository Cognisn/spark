"""project_cards.js under Node, and the card hook's static guarantees (PC17, PC18).

* Parsing a card line: only a well-formed id, a whole-number conversation and a known
  kind; anything else (markup, a URL, an unknown kind) is no card.
* Buttons: Create, Edit and Dismiss for a pending suggestion; Confirm and Cancel for an
  update or a move; none for a card that is decided, expired, or someone else's.
* What a card says: before-and-after rows for an update, the move warning with names
  (widen, narrow, shares ending, nobody affected), its state line.
* Accepting sends a suggestion's edits only, and only the three editable fields.
* The page part sets every card value as text (no markup from the payload), fetches only
  the cards API, and chat.js loads project_cards.js from SPARK only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_preview_frontend import CDN_HOSTS

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
JS = WEB / "static" / "js"
CARDS_JS = JS / "project_cards.js"
CARDS_CSS = WEB / "static" / "css" / "project_cards.css"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not installed")

ID = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"

_SCRIPT = r"""
const c = require(process.argv[1]);
const id = process.argv[2];
const line = (o) => '[spark-project-card] ' + JSON.stringify(o);
const view = (kind, state, mine) => ({id, kind, state, mine, can_act: mine && state === 'pending'});
const out = {
  parse: [
    c.parse('before\n' + line({card: id, conversation_id: 7, kind: 'suggestion'}) + '\nafter'),
    c.parse(line({card: '<img src=x>', conversation_id: 7, kind: 'suggestion'})),
    c.parse(line({card: id, conversation_id: '7x', kind: 'move'})),
    c.parse(line({card: id, conversation_id: 7, kind: 'delete'})),
    c.parse('[spark-project-card] not json'),
    c.parse('nothing'),
  ],
  urls: [c.cardUrl(id), c.cardUrl(id, 'accept'), c.cardUrl(id, 'dismiss'), c.cardUrl('a/b', 'evil')],
  tools: c.TOOLS.map(c.isTool).concat([c.isTool('create_chart')]),
  actions: [
    c.actions(view('suggestion', 'pending', true)).map(a => a.id),
    c.actions(view('update', 'pending', true)).map(a => a.id),
    c.actions(view('move', 'pending', true)).map(a => a.id),
    c.actions(view('suggestion', 'pending', false)).map(a => a.id),
    c.actions(view('suggestion', 'accepted', true)).map(a => a.id),
    c.actions(view('update', 'expired', true)).map(a => a.id),
    c.actions(null),
  ],
  states: [
    c.stateLabel(view('suggestion', 'pending', true)),
    c.stateLabel(view('suggestion', 'pending', false)),
    c.stateLabel(view('suggestion', 'accepted', true)),
    c.stateLabel(view('suggestion', 'dismissed', true)),
    c.stateLabel(view('update', 'accepted', true)),
    c.stateLabel(view('move', 'accepted', true)),
    c.stateLabel(view('move', 'cancelled', true)),
    c.stateLabel(view('update', 'expired', true)),
  ],
  diff: c.diffRows({before: {name: 'Plan', instructions: ''}, after: {name: 'Plan B', instructions: 'Be brief.'}}),
  warnings: [
    c.warning({widened: {g1: 'viewer'}, narrowed: {}, shares_removed: [], names: {g1: 'Cora'}}),
    c.warning({widened: {}, narrowed: {g2: null, g3: 'viewer'}, shares_removed: ['g2', 'g4'],
               names: {g2: 'Vic', g3: 'Pat'}}),
    c.warning({widened: {}, narrowed: {}, shares_removed: []}),
  ],
  destination: [c.destination({to: {id: 1, name: 'Plan'}}), c.destination({to: null})],
  bodies: [
    c.acceptBody(view('suggestion', 'pending', true), {name: 'N', instructions: 'I', evil: 'x', description: 3}),
    c.acceptBody(view('update', 'pending', true), {name: 'N'}),
    c.acceptBody(view('suggestion', 'pending', true), undefined),
  ],
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("Node is not installed")
    done = subprocess.run(
        [NODE, "-e", _SCRIPT, str(CARDS_JS), ID],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@needs_node
def test_parsing_a_card_line(node: dict[str, Any]) -> None:
    assert node["parse"][0] == {"card": ID, "conversation_id": 7, "kind": "suggestion"}
    assert node["parse"][1:] == [None, None, None, None, None]


@needs_node
def test_urls_and_tools(node: dict[str, Any]) -> None:
    assert node["urls"] == [
        f"/project-cards/api/{ID}",
        f"/project-cards/api/{ID}/accept",
        f"/project-cards/api/{ID}/dismiss",
        "/project-cards/api/a%2Fb/dismiss",
    ]
    assert node["tools"] == [True, True, True, True, False]


@needs_node
def test_buttons(node: dict[str, Any]) -> None:
    assert node["actions"] == [
        ["create", "edit", "dismiss"],
        ["confirm", "cancel"],
        ["confirm", "cancel"],
        [],
        [],
        [],
        [],
    ]


@needs_node
def test_state_lines(node: dict[str, Any]) -> None:
    assert node["states"] == [
        "Nothing changes until you choose.",
        "Waiting for the person who asked.",
        "Created",
        "Dismissed",
        "Confirmed",
        "Moved",
        "Cancelled",
        "Expired: ask again if you still want this.",
    ]


@needs_node
def test_before_and_after(node: dict[str, Any]) -> None:
    assert node["diff"] == [
        {"field": "name", "label": "Name", "before": "Plan", "after": "Plan B"},
        {"field": "instructions", "label": "Instructions", "before": "", "after": "Be brief."},
    ]


@needs_node
def test_the_move_warning(node: dict[str, Any]) -> None:
    widen, narrow, none = node["warnings"]
    assert widen == {"level": "widen", "lines": ["Cora would see it as Read-only."]}
    assert narrow["level"] == "narrow"
    assert narrow["lines"] == [
        "Vic would no longer see it.",
        "Pat would keep it only as Read-only.",
        "Someone would lose the share you gave them.",
    ]
    assert none == {"level": "none", "lines": []}
    assert node["destination"] == ['Into the project "Plan"', "Out of its project"]


@needs_node
def test_accepting_sends_only_a_suggestions_edits(node: dict[str, Any]) -> None:
    assert node["bodies"] == [{"edits": {"name": "N", "instructions": "I"}}, {}, {}]


def _page_part() -> str:
    source = CARDS_JS.read_text(encoding="utf-8")
    return source[source.index("// -- The page") :]


def test_card_values_are_text_never_markup() -> None:
    page = _page_part()
    # The only markup assignment clears the card.
    assert re.findall(r"innerHTML\s*=\s*([^;]+);", page) == ["''"]
    assert "insertAdjacentHTML" not in page and "outerHTML" not in page
    assert "textContent" in page


def test_fetches_only_the_cards_api() -> None:
    source = CARDS_JS.read_text(encoding="utf-8")
    assert re.findall(r"fetch\(([^,)]+)", source) == ["cardUrl(view.id", "cardUrl(ref.card"]
    assert "const API = '/project-cards/api/';" in source


def test_no_cdn_and_loaded_from_spark() -> None:
    for path in (CARDS_JS, CARDS_CSS):
        text = path.read_text(encoding="utf-8")
        assert not CDN_HOSTS.search(text), path
        assert "http://" not in text and "https://" not in text, path
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    assert "src: '/static/js/project_cards.js'" in chat
    assert "SparkProjectCards.fill(slot, text)" in chat
