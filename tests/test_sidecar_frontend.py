"""sidecar.js under Node, and the panel's static guarantees (PC2 to PC5).

* Default states: Artefacts expanded, Tools and Agents collapsed; a section's choice is
  remembered per section in the browser, and every storage call is guarded so the panel
  works when storage throws (or there is none).
* A section expands while something runs in it and then stays as the user leaves it; the
  expansion is not remembered as the user's choice.
* The panel opens by itself the first time an artefact appears in a conversation (once),
  and its button shows a dot for new items while it is closed; opening clears the dot.
* While the preview pane is open the panel narrows (desktop) or slides aside (phone) and
  returns when the pane closes.
* The listing becomes groups: Generated or retrieved, Provided (by you, or labelled by
  person in a shared conversation), and Project resources by level; each item gives its
  title with file name and type beneath, and opens as a card in the preview pane.
* A title is checked before it is sent; the message an item came from is found, or the
  nearest earlier one.
* The page part sets every value as text, never as markup; it loads no CDN; the sidecar
  is its own template, script and style, and the old tabs are gone.
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
SIDECAR_JS = JS / "sidecar.js"
SIDECAR_CSS = WEB / "static" / "css" / "sidecar.css"
TEMPLATES = WEB / "templates"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not installed")

_SCRIPT = r"""
const s = require(process.argv[1]);

function store(initial, throwing) {
  const data = Object.assign({}, initial || {});
  const calls = [];
  return {
    data, calls,
    getItem(k) { calls.push(['get', k]); if (throwing) throw new Error('blocked'); return k in data ? data[k] : null; },
    setItem(k, v) { calls.push(['set', k]); if (throwing) throw new Error('blocked'); data[k] = String(v); },
  };
}
const open = (m) => s.SECTIONS.map(x => m.isOpen(x));
const out = {};

// -- Default states and remembered choices
{
  const local = store();
  const m = s.createModel({ storage: local, conversationId: 7 });
  out.defaults = open(m);
  m.toggle('tools'); m.toggle('artefacts');
  out.afterToggle = open(m);
  out.saved = local.data;
  const again = s.createModel({ storage: local, conversationId: 8 });
  out.restored = open(again);
}
// -- Storage that throws, and no storage at all
{
  const m = s.createModel({ storage: store({}, true), session: store({}, true), conversationId: 7 });
  out.throwing = [open(m)];
  m.toggle('agents'); out.throwing.push(open(m));
  m.openPanel(); out.throwing.push(m.panelOpen);
  m.noteArtefacts([1], true); out.throwing.push(m.panelOpen);
  const none = s.createModel({ conversationId: 7 });
  none.toggle('tools'); out.none = open(none);
}
// -- Garbage in storage is ignored
{
  const m = s.createModel({ storage: store({'spark-sidecar-section-tools': 'maybe', 'spark-sidecar-section-agents': 'open'}), conversationId: 7 });
  out.garbage = open(m);
}
// -- Auto-expand while running, stays, and is not remembered
{
  const local = store();
  const m = s.createModel({ storage: local, conversationId: 7 });
  out.run = [m.run('tools'), m.isOpen('tools'), m.run('tools')];
  out.runKeys = Object.keys(local.data);
  m.toggle('tools');   // the user collapses it
  out.userCollapsed = [m.isOpen('tools'), local.data['spark-sidecar-section-tools']];
  m.run('tools');      // a new run opens it again
  out.reopened = m.isOpen('tools');
  const next = s.createModel({ storage: local, conversationId: 7 });
  out.nextVisit = next.isOpen('tools');
}
// -- Counts
{
  const m = s.createModel({ conversationId: 7 });
  m.setCount('artefacts', 3); m.setCount('tools', 2); m.setCount('agents', -4); m.setCount('x', 9);
  out.counts = s.SECTIONS.map(x => m.count(x));
}
// -- The panel opens for the first artefact only
{
  const local = store();
  const m = s.createModel({ storage: local, session: store(), conversationId: 7 });
  out.first = [m.noteArtefacts([], true), m.panelOpen];
  out.first.push(m.noteArtefacts([4], false), m.panelOpen, m.dot);
  m.closePanel();
  out.first.push(m.noteArtefacts([4, 5], false), m.panelOpen, m.dot);
  const reload = s.createModel({ storage: local, session: store(), conversationId: 7 });
  out.reload = [reload.noteArtefacts([4, 5], true), reload.panelOpen, reload.dot];
  const other = s.createModel({ storage: local, session: store(), conversationId: 9 });
  out.otherConversation = [other.noteArtefacts([4, 5], true), other.panelOpen];
}
// -- The dot
{
  const m = s.createModel({ storage: store(), session: store(), conversationId: 7 });
  m.noteArtefacts([1], true); m.closePanel();
  out.dot = [m.dot];
  m.noteArtefacts([1], false); out.dot.push(m.dot);          // nothing new
  m.noteArtefacts([1, 2], false); out.dot.push(m.dot);       // something new
  m.openPanel(); out.dot.push(m.dot);                        // opening clears it
  m.noteArtefacts([1, 2, 3], false); out.dot.push(m.dot);    // open: no dot
  m.closePanel(); m.noteItem('tools'); out.dot.push(m.dot);  // a tool call while closed
  m.openPanel(); m.noteItem('agents'); out.dot.push(m.dot);
}
// -- The panel and the preview pane
{
  const m = s.createModel({ storage: store(), session: store(), conversationId: 7 });
  out.layout = [m.layout()];
  m.openPanel(); out.layout.push(m.layout());
  m.setPane(true); out.layout.push(m.layout());
  m.setPhone(true); out.layout.push(m.layout());
  m.setPane(false); out.layout.push(m.layout());
  m.setPhone(false); m.setPane(true); m.closePanel(); out.layout.push(m.layout());
}
// -- Panel state survives a reload in the same tab
{
  const session = store();
  const a = s.createModel({ session, conversationId: 7 });
  a.openPanel();
  const b = s.createModel({ session, conversationId: 7 });
  out.panelKept = b.panelOpen;
  b.closePanel();
  out.panelClosed = s.createModel({ session, conversationId: 7 }).panelOpen;
}

// -- The listing as groups
const item = (id, extra) => Object.assign({
  file_id: id, title: 'Title ' + id, name: 'file' + id + '.pdf', type: 'application/pdf', kind: 'document',
  preview_kind: 'pdf', size: 2048, created_at: '2026-11-26T01:02:03+00:00', message_id: id * 10,
  can_rename: true, provided_by: null }, extra || {});
const payload = {
  conversation_id: 7, shared: true, can_rename: true,
  generated: [item(1), item(2, { preview_kind: 'chart', kind: 'chart', type: 'application/vnd.spark.chart+json' })],
  provided: [item(3, { provided_by: { guid: 'g1', name: 'Cora Example', initials: 'CE', is_me: false } }),
             item(4, { provided_by: { guid: 'g2', name: 'Ada', initials: 'A', is_me: true } })],
  project: [{ project_id: 1, name: 'Plan', level: 1, items: [item(5, { can_rename: false, message_id: null })] },
            { project_id: 2, name: 'Detail', level: 2, items: [item(6, { can_rename: false, message_id: null })] }],
};
out.groups = s.groupsOf(payload).map(g => [g.id, g.label, g.items.map(i => i.file_id), g.count]);
out.groupsAlone = s.groupsOf(Object.assign({}, payload, { shared: false, project: [] })).map(g => [g.id, g.label]);
out.empty = s.groupsOf({ generated: [], provided: [], project: [] }).map(g => g.id);
out.total = s.totalOf(payload);
out.totalBroken = [s.totalOf(null), s.totalOf({}), s.totalOf({ generated: 'x' })];
out.meta = [
  s.itemMeta(payload.generated[0], { locale: 'en-AU', timeZone: 'UTC' }),
  s.itemMeta(payload.provided[0], { locale: 'en-AU', timeZone: 'UTC' }),
  s.itemMeta(payload.provided[1], { locale: 'en-AU', timeZone: 'UTC' }),
  s.itemMeta(item(9, { size: 5, created_at: 'not a date', type: '' }), { locale: 'en-AU', timeZone: 'UTC' }),
];
out.card = s.cardOf(payload.generated[1], 7);
out.cardBad = [s.cardOf(null, 7), s.cardOf({ file_id: 'x' }, 7)];
out.nearest = [
  s.nearestMessage(30, [10, 20, 30, 40]), s.nearestMessage(35, [10, 20, 30, 40]),
  s.nearestMessage(5, [10, 20]), s.nearestMessage(null, [10, 20]), s.nearestMessage(10, []),
];
out.titles = [s.checkTitle('Quarterly sales'), s.checkTitle('  '), s.checkTitle('x'.repeat(121)),
              s.checkTitle('x'.repeat(120)), s.checkTitle(7), s.checkTitle(' Plan  ')];
out.urls = [s.listUrl(7), s.renameUrl(12), s.renameUrl('a/b')];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("Node is not installed")
    done = subprocess.run(
        [NODE, "-e", _SCRIPT, str(SIDECAR_JS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@needs_node
def test_default_states_and_remembered_choices(node: dict[str, Any]) -> None:
    assert node["defaults"] == [True, False, False]
    assert node["afterToggle"] == [False, True, False]
    assert node["saved"] == {
        "spark-sidecar-section-tools": "open",
        "spark-sidecar-section-artefacts": "closed",
    }
    assert node["restored"] == [False, True, False]


@needs_node
def test_works_when_storage_throws_or_is_missing(node: dict[str, Any]) -> None:
    assert node["throwing"] == [[True, False, False], [True, False, True], True, True]
    assert node["none"] == [True, True, False]
    assert node["garbage"] == [True, False, True]


@needs_node
def test_a_section_expands_while_running_and_stays(node: dict[str, Any]) -> None:
    assert node["run"] == [True, True, False]
    assert node["runKeys"] == []
    assert node["userCollapsed"] == [False, "closed"]
    assert node["reopened"] is True
    assert node["nextVisit"] is False


@needs_node
def test_counts(node: dict[str, Any]) -> None:
    assert node["counts"] == [3, 2, 0]


@needs_node
def test_the_panel_opens_for_the_first_artefact_only(node: dict[str, Any]) -> None:
    assert node["first"] == [
        {"opened": False, "dot": False},
        False,
        {"opened": True, "dot": False},
        True,
        False,
        {"opened": False, "dot": True},
        False,
        True,
    ]
    assert node["reload"] == [{"opened": False, "dot": False}, False, False]
    assert node["otherConversation"] == [{"opened": True, "dot": False}, True]


@needs_node
def test_the_dot_for_new_items(node: dict[str, Any]) -> None:
    assert node["dot"] == [False, False, True, False, False, True, False]


@needs_node
def test_the_panel_gives_way_to_the_preview_pane(node: dict[str, Any]) -> None:
    assert node["layout"] == ["hidden", "full", "narrow", "aside", "full", "hidden"]
    assert node["panelKept"] is True and node["panelClosed"] is False


@needs_node
def test_the_listing_becomes_groups(node: dict[str, Any]) -> None:
    assert node["groups"] == [
        ["generated", "Generated or retrieved", [1, 2], 2],
        ["provided", "Provided", [3, 4], 2],
        ["project-1", "Project resources: Plan", [5], 1],
        ["project-2", "Project resources: Detail", [6], 1],
    ]
    assert node["groupsAlone"] == [
        ["generated", "Generated or retrieved"],
        ["provided", "Provided by you"],
    ]
    assert node["empty"] == ["generated", "provided"]
    assert node["total"] == 6
    assert node["totalBroken"] == [0, 0, 0]


@needs_node
def test_an_item_gives_title_file_name_type_and_time(node: dict[str, Any]) -> None:
    generated, theirs, mine, odd = node["meta"]
    for entry in node["meta"]:
        entry["when"] = entry["when"].replace(chr(0x202F), " ").replace(chr(0xA0), " ")
    assert generated == {
        "title": "Title 1",
        "detail": "file1.pdf",
        "type": "PDF",
        "size": "2.0 KB",
        "when": "26 Nov 2026, 1:02 am",
        "by": "",
    }
    assert theirs["by"] == "Attached by Cora Example"
    assert mine["by"] == "Attached by you"
    assert odd["when"] == "" and odd["size"] == "5 bytes" and odd["type"] == "PDF"


@needs_node
def test_an_item_opens_as_a_card_in_the_pane(node: dict[str, Any]) -> None:
    assert node["card"] == {
        "file_id": 2,
        "conversation_id": 7,
        "name": "file2.pdf",
        "title": "Title 2",
        "type": "application/vnd.spark.chart+json",
        "kind": "chart",
        "size": 2048,
    }
    assert node["cardBad"] == [None, None]


@needs_node
def test_the_origin_message_or_the_nearest_earlier_one(node: dict[str, Any]) -> None:
    assert node["nearest"] == [30, 30, None, None, None]


@needs_node
def test_a_title_is_checked_before_it_is_sent(node: dict[str, Any]) -> None:
    ok, blank, long_, edge, number, trimmed = node["titles"]
    assert ok == {"ok": True, "title": "Quarterly sales"}
    assert blank["ok"] is False and "required" in blank["error"]
    assert long_["ok"] is False and "120" in long_["error"]
    assert edge["ok"] is True
    assert number["ok"] is False
    assert trimmed == {"ok": True, "title": "Plan"}
    assert node["urls"] == ["/artefacts/api/7", "/artefacts/api/12", "/artefacts/api/a%2Fb"]


def _page_part() -> str:
    source = SIDECAR_JS.read_text(encoding="utf-8")
    return source[source.index("// -- The page") :]


def test_values_are_text_never_markup() -> None:
    page = _page_part()
    assert re.findall(r"innerHTML\s*=\s*([^;]+);", page) == []
    assert "insertAdjacentHTML" not in page and "outerHTML" not in page
    assert "textContent" in page


def test_every_storage_call_is_guarded() -> None:
    source = SIDECAR_JS.read_text(encoding="utf-8")
    calls = list(re.finditer(r"(?:storage|localStorage|sessionStorage)\.(?:get|set)Item\(", source))
    assert len(calls) == 2
    for call in calls:
        assert "try {" in source[max(0, call.start() - 60) : call.start()], call.group(0)
    # Storage is reached only through the two guarded helpers.
    assert (
        len(re.findall(r"readItem\(", source)) > 3 and len(re.findall(r"writeItem\(", source)) > 3
    )


def test_the_page_fetches_only_the_artefacts_api() -> None:
    source = SIDECAR_JS.read_text(encoding="utf-8")
    assert re.findall(r"fetch\(([^,)]+)", source) == [
        "renameUrl(item.file_id",
        "listUrl(state.conversationId",
    ]
    assert "const API = '/artefacts/api/';" in source


def test_no_cdn_and_loaded_from_spark() -> None:
    for path in (SIDECAR_JS, SIDECAR_CSS, TEMPLATES / "_sidecar.html"):
        text = path.read_text(encoding="utf-8")
        assert not CDN_HOSTS.search(text), path
        assert "http://" not in text and "https://" not in text, path
    chat = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
    assert '{% include "_sidecar.html" %}' in chat
    assert '<script src="/static/js/sidecar.js"></script>' in chat
    assert '<link rel="stylesheet" href="/static/css/sidecar.css">' in chat


def test_the_tabs_are_replaced_by_three_sections() -> None:
    html = (TEMPLATES / "_sidecar.html").read_text(encoding="utf-8")
    chat = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
    assert "switchSidecarTab" not in html + chat + (JS / "chat.js").read_text(encoding="utf-8")
    assert "sidecar-tab" not in html + chat
    for section in ("artefacts", "tools", "agents"):
        assert f'id="sidecar-{section}-toggle"' in html
        assert f'aria-controls="sidecar-{section}-body"' in html
    assert len(re.findall(r'aria-expanded="(?:true|false)"', html)) == 3
    assert len(re.findall(r"<h3\b", html)) == 3
    # The ids the tool and agent rendering writes into are kept.
    for kept in ("tool-panel", "tool-panel-body", "agent-panel-body", "sidecar-resize-handle"):
        assert f'id="{kept}"' in html


def test_chat_js_keeps_the_event_contract() -> None:
    chat = (JS / "chat.js").read_text(encoding="utf-8")
    for name in (
        "_addToolPanelItem",
        "updateToolPanelItem",
        "_addAgentPanelItem",
        "appendStreamingToolStart",
        "appendStreamingAgentStart",
        "updateStreamingAgentComplete",
    ):
        assert f"function {name}(" in chat
    # The resize functions moved to sidecar.js.
    assert "function startSidecarResize" not in chat
    assert "function startSidecarResize" in SIDECAR_JS.read_text(encoding="utf-8")
