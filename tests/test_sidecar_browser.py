"""The right-hand panel in a real browser (PC2 to PC5), on the real clock.

A headless Chromium opens a harness page with the real panel (``_sidecar.html``,
``sidecar.js``, ``sidecar.css``), the real preview pane and ``preview.js``, and the real
``chat.js`` drawing tool calls and agent runs. The harness serves ``/artefacts/api/1``
from a small in-memory list (its PATCH renames) and the files the preview pane opens, and
the page posts its results back when it has finished (decision 215: no virtual time).

Desktop:

* The first artefact opens the panel by itself; Artefacts open, Tools and Agents closed,
  counts on every header, groups with their titles, items with title, file name and type.
* A section's button toggles it and keeps the focus; the choice is remembered; a tool call
  and an agent run expand their sections without overwriting the choice, light the dot
  while the panel is closed, and opening the panel clears it.
* An item opens in the preview pane, scrolls to and marks its message, and the panel
  narrows while the pane is open and returns when it closes.
* Rename: the pencil only where the list says so; a blank title is refused before a
  request; a good one is sent as PATCH and shown.

Phone: a full-screen overlay that gives way to the pane and closes on Escape.

Storage that throws: the panel still opens, toggles and renames.
"""

from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader

from spark.web.endpoints.preview import FRAME_POLICY
from tests.test_preview_renderers import BROWSER, stop_browser

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"
RUN_BUDGET_S = 240

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(RUN_BUDGET_S + 90),
]


def listing() -> dict[str, Any]:
    def item(fid: int, name: str, kind: str, mime: str, message: int | None, **kw: Any) -> Any:
        return {
            "file_id": fid,
            "title": kw.pop("title", name),
            "name": name,
            "type": mime,
            "kind": "attachment" if kw.get("provided_by") else "document",
            "preview_kind": kind,
            "size": 2048,
            "created_at": "2026-11-26T01:02:03+00:00",
            "message_id": message,
            "can_rename": kw.pop("can_rename", True),
            "provided_by": kw.pop("provided_by", None),
        }

    return {
        "conversation_id": 1,
        "shared": True,
        "can_rename": True,
        "generated": [item(101, "notes.txt", "text", "text/plain", 12, title="Meeting notes")],
        "provided": [
            item(
                102,
                "plan.md",
                "markdown",
                "text/markdown",
                11,
                provided_by={
                    "guid": "g1",
                    "name": "Cora Example",
                    "initials": "CE",
                    "is_me": False,
                },
            )
        ],
        "project": [
            {
                "project_id": 5,
                "name": "Plan",
                "level": 1,
                "items": [item(103, "rules.txt", "text", "text/plain", None, can_rename=False)],
            }
        ],
        "counts": {"generated": 1, "provided": 1, "project": 1},
    }


PAGE = """<!DOCTYPE html>
<html lang="en" data-bs-theme="{theme}"><head><meta charset="utf-8"><title>sidecar</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="/static/css/cognisn.css" rel="stylesheet">
<link href="/static/css/base.css" rel="stylesheet">
<link href="/static/css/spark.css" rel="stylesheet">
<link rel="stylesheet" href="/static/css/preview.css">
<link rel="stylesheet" href="/static/css/sidecar.css">
{blocked}
</head><body style="margin:0">
<div style="display:flex;height:100vh">
<div style="flex:1;min-width:0;display:flex;flex-direction:column">
<div style="padding:4px"><button type="button" class="btn btn-app-ghost btn-sm" id="btn-tool-panel"
  aria-controls="tool-panel" aria-expanded="false">Panel<span id="sidecar-dot" class="sidecar-dot" hidden></span></button></div>
<div id="chat-messages" style="flex:1;overflow:auto;padding:8px"></div></div>
{pane}{sidecar}</div>
<pre id="results">pending</pre>
<script src="/static/vendor/marked/marked.umd.js"></script>
<script src="/static/vendor/dompurify/purify.min.js"></script>
<script src="/static/js/markdown.js"></script>
<script src="/static/js/main.js"></script>
<script src="/static/js/preview.js"></script>
<script src="/static/js/sidecar.js"></script>
<script src="/static/js/chat.js"></script>
<script>
const MODE = '{mode}';
const out = {{ errors: [] }};
window.addEventListener('error', e => out.errors.push(String(e.message)));
function wait(check, ms) {{
  return new Promise(resolve => {{
    const start = Date.now();
    (function poll() {{
      let value = null;
      try {{ value = check(); }} catch (err) {{ value = null; }}
      if (value || Date.now() - start > ms) return resolve(value);
      setTimeout(poll, 50);
    }})();
  }});
}}
function report(o) {{
  document.getElementById('results').textContent = JSON.stringify(o);
  return fetch('/report', {{ method: 'POST', body: JSON.stringify(o) }});
}}
const $ = id => document.getElementById(id);
const panel = () => $('tool-panel');
const expanded = () => ['artefacts', 'tools', 'agents'].map(n => $('sidecar-' + n + '-toggle').getAttribute('aria-expanded'));
const counts = () => ['artefacts', 'tools', 'agents'].map(n => $('sidecar-' + n + '-count').textContent);
const store = key => {{ try {{ return localStorage.getItem(key); }} catch (e) {{ return 'blocked'; }} }};
const row = id => document.querySelector('.sidecar-item[data-file-id="' + id + '"]');
const layout = () => panel().dataset.layout;

(async function () {{
  try {{
    appendMessage({{ id: 11, role: 'user', content: 'Please read this plan' }});
    appendMessage({{ id: 12, role: 'assistant', content: 'I made some notes' }});
    appendMessage({{ id: 13, role: 'user', content: 'Thanks' }});
    out.tagged = [...document.querySelectorAll('#chat-messages > [data-message-id]')].map(n => n.dataset.messageId);
    SparkSidecar.init({{ conversationId: 1 }});
    await wait(() => document.querySelectorAll('.sidecar-item').length >= 3, 30000);
    const panelEl = panel();
    out.initial = {{
      hidden: panelEl.hidden, layout: layout(), expanded: expanded(), counts: counts(),
      dotHidden: $('sidecar-dot').hidden, button: $('btn-tool-panel').getAttribute('aria-expanded'),
      groups: [...document.querySelectorAll('.sidecar-group-title')].map(n => n.textContent),
      meetingTitle: row(101).querySelector('.sidecar-item-title').textContent,
      meetingDetail: row(101).querySelector('.sidecar-item-detail').textContent,
      byLine: row(102).querySelector('.sidecar-item-when').textContent.split(' · ')[0],
      renameButtons: [101, 102, 103].map(id => !!row(id).querySelector('.sidecar-rename')),
      headings: [...panelEl.querySelectorAll('h3')].length,
      openLabel: row(101).querySelector('.sidecar-open').getAttribute('aria-label'),
    }};

    // Toggle with the focus kept, and remembered
    const toolsToggle = $('sidecar-tools-toggle');
    toolsToggle.focus();
    toolsToggle.click();
    out.toggle = {{
      expanded: expanded(), bodyHidden: $('sidecar-tools-body').hidden,
      focusKept: document.activeElement === toolsToggle, stored: store('spark-sidecar-section-tools'),
    }};
    toolsToggle.click();
    out.toggleBack = {{ expanded: expanded(), stored: store('spark-sidecar-section-tools') }};

    // A tool call and an agent run while the panel is closed
    $('sidecar-close').click();
    out.closed = {{ hidden: panelEl.hidden, focusBack: document.activeElement === $('btn-tool-panel'),
      button: $('btn-tool-panel').getAttribute('aria-expanded') }};
    appendStreamingToolStart('web_search', {{ q: 'x' }}, 't1');
    out.tool = {{ expanded: expanded(), counts: counts(), dotHidden: $('sidecar-dot').hidden,
      stored: store('spark-sidecar-section-tools'), items: document.querySelectorAll('#tool-panel-body .tool-panel-item').length }};
    updateStreamingToolComplete('t1', 'web_search', 'ok', 'success');
    appendStreamingAgentStart('Researcher', 'a1', 'look it up', 'm');
    out.agent = {{ expanded: expanded(), counts: counts(),
      items: document.querySelectorAll('#agent-panel-body .agent-panel-item').length }};
    $('btn-tool-panel').click();
    out.reopened = {{ hidden: panelEl.hidden, dotHidden: $('sidecar-dot').hidden, layout: layout() }};

    // Open an item: the pane, the message, and the panel gives way
    const widthBefore = panelEl.getBoundingClientRect().width;
    row(101).querySelector('.sidecar-open').click();
    await wait(() => !$('preview-pane').hidden && layout() !== 'full', 20000);
    const pane = $('preview-pane');
    const marked = document.querySelector('.sidecar-origin');
    out.open = {{
      paneHidden: pane.hidden, tabs: [...document.querySelectorAll('.preview-tab span')].map(n => n.textContent),
      layout: layout(), widthBefore, widthAfter: panelEl.getBoundingClientRect().width,
      panelHidden: panelEl.hidden, marked: marked ? marked.dataset.messageId : null,
    }};
    $('preview-pane-close').click();
    await wait(() => layout() === 'full', 20000);
    out.afterClose = {{ layout: layout(), hidden: panelEl.hidden, widthAfter: panelEl.getBoundingClientRect().width }};

    // An item with no message opens without a mark; a resource opens too
    row(103).querySelector('.sidecar-open').click();
    await wait(() => !$('preview-pane').hidden, 20000);
    out.resource = {{ tabs: [...document.querySelectorAll('.preview-tab span')].map(n => n.textContent) }};
    $('preview-pane-close').click();
    await wait(() => layout() === 'full', 20000);

    // Rename: a blank title is refused before a request; a good one is sent and shown
    row(101).querySelector('.sidecar-rename').click();
    let input = row(101).querySelector('.sidecar-rename-form input');
    input.value = '   ';
    row(101).querySelector('.sidecar-rename-form').requestSubmit();
    out.blank = {{ error: row(101).querySelector('.sidecar-rename-error').textContent,
      errorShown: !row(101).querySelector('.sidecar-rename-error').hidden }};
    input.value = '  Quarterly notes ';
    row(101).querySelector('.sidecar-rename-form').requestSubmit();
    await wait(() => row(101) && row(101).querySelector('.sidecar-item-title') &&
      row(101).querySelector('.sidecar-item-title').textContent === 'Quarterly notes', 20000);
    out.renamed = {{ title: row(101).querySelector('.sidecar-item-title').textContent,
      form: !!row(101).querySelector('.sidecar-rename-form'),
      focus: document.activeElement && document.activeElement.className }};

    if (MODE === 'phone') {{
      out.phone = {{
        position: getComputedStyle(panelEl).position, width: panelEl.getBoundingClientRect().width,
        viewport: document.documentElement.clientWidth, role: panelEl.getAttribute('role'), modal: panelEl.getAttribute('aria-modal'),
      }};
      row(101).querySelector('.sidecar-open').click();
      await wait(() => !$('preview-pane').hidden, 20000);
      out.phone.withPane = {{ layout: layout(), hidden: panelEl.hidden }};
      $('preview-pane-close').click();
      await wait(() => layout() === 'full', 20000);
      out.phone.afterPane = {{ layout: layout(), hidden: panelEl.hidden }};
      panelEl.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
      out.phone.escape = {{ hidden: panelEl.hidden }};
    }}
  }} catch (err) {{
    out.errors.push(String(err && err.stack || err));
  }}
  await report(out);
}})();
</script></body></html>"""

BLOCKED = (
    "<script>for (const name of ['localStorage', 'sessionStorage']) {"
    " Object.defineProperty(window, name, { get() { throw new DOMException('blocked', 'SecurityError'); } }); }"
    "</script>"
)


class Harness:
    def __init__(self, mode: str, theme: str = "light") -> None:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        frame = env.get_template("preview_frame.html").render().encode("utf-8")
        pane = env.get_template("_preview_pane.html").render(conversation={"id": 1})
        sidecar = env.get_template("_sidecar.html").render()
        page = PAGE.format(
            theme=theme,
            pane=pane,
            sidecar=sidecar,
            mode=mode,
            blocked=BLOCKED if mode == "blocked" else "",
        ).encode("utf-8")
        data = listing()
        self.patches: list[dict[str, Any]] = []
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        harness = self

        def items() -> list[dict[str, Any]]:
            found = data["generated"] + data["provided"]
            for level in data["project"]:
                found += level["items"]
            return found  # type: ignore[no-any-return]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: bytes, kind: str, **headers: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name.replace("_", "-"), value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/report":
                    harness.report = json.loads(body)
                    harness.reported.set()
                self._send(204, b"", "text/plain")

            def do_PATCH(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                match = re.fullmatch(r"/artefacts/api/(\d+)", self.path)
                found = [i for i in items() if match and i["file_id"] == int(match.group(1))]
                if not found:
                    self._send(404, b"{}", "application/json")
                    return
                harness.patches.append({"file_id": found[0]["file_id"], **body})
                found[0]["title"] = body["title"]
                answer = {"file_id": found[0]["file_id"], "title": body["title"]}
                self._send(200, json.dumps(answer).encode(), "application/json")

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/harness":
                    self._send(200, page, "text/html; charset=utf-8")
                elif path == "/preview/frame":
                    self._send(
                        200, frame, "text/html; charset=utf-8", Content_Security_Policy=FRAME_POLICY
                    )
                elif path == "/artefacts/api/1":
                    self._send(200, json.dumps(data).encode(), "application/json")
                elif re.fullmatch(r"/preview/api/1/files/\d+", path):
                    self._send(200, b"hello", "application/octet-stream")
                elif path.startswith("/static/"):
                    target = (STATIC / path[len("/static/") :]).resolve()
                    if not target.is_file() or STATIC.resolve() not in target.parents:
                        self._send(404, b"", "text/plain")
                        return
                    kind = (
                        "text/javascript"
                        if target.suffix in (".js", ".mjs")
                        else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    )
                    self._send(200, target.read_bytes(), kind)
                else:
                    self._send(404, b"", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run(mode: str, size: str, theme: str = "light") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    assert BROWSER is not None
    harness = Harness(mode, theme)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-sidecar-", ignore_cleanup_errors=True
        ) as profile:
            browser = subprocess.Popen(
                [
                    BROWSER,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--window-size={size}",
                    f"--user-data-dir={profile}",
                    f"http://127.0.0.1:{harness.port}/harness",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = harness.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, "the harness did not finish"
        return harness.report, harness.patches
    finally:
        harness.close()


@pytest.fixture(scope="module")
def desktop() -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    yield run("desktop", "1400,900")


@pytest.fixture(scope="module")
def phone() -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    yield run("phone", "390,844")


@pytest.fixture(scope="module")
def blocked() -> Iterator[tuple[dict[str, Any], list[dict[str, Any]]]]:
    yield run("blocked", "1400,900", theme="dark")


def test_the_first_artefact_opens_the_panel_with_its_groups(
    desktop: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, _ = desktop
    assert out["errors"] == []
    assert out["tagged"] == ["11", "12", "13"]
    first = out["initial"]
    assert first["hidden"] is False and first["layout"] == "full"
    assert first["expanded"] == ["true", "false", "false"]
    assert first["counts"] == ["3", "0", "0"]
    assert first["dotHidden"] is True and first["button"] == "true"
    assert first["groups"] == [
        "Generated or retrieved (1)",
        "Provided (1)",
        "Project resources: Plan (1)",
    ]
    assert first["meetingTitle"] == "Meeting notes"
    assert first["meetingDetail"] == "notes.txt · Text · 2.0 KB"
    assert first["byLine"] == "Attached by Cora Example"
    assert first["renameButtons"] == [True, True, False]
    assert first["headings"] == 3
    assert first["openLabel"] == "Open Meeting notes in the document preview"


def test_sections_toggle_with_focus_kept_and_are_remembered(
    desktop: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, _ = desktop
    assert out["toggle"] == {
        "expanded": ["true", "true", "false"],
        "bodyHidden": False,
        "focusKept": True,
        "stored": "open",
    }
    assert out["toggleBack"] == {"expanded": ["true", "false", "false"], "stored": "closed"}


def test_running_expands_a_section_without_changing_the_choice(
    desktop: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, _ = desktop
    assert out["closed"] == {"hidden": True, "focusBack": True, "button": "false"}
    assert out["tool"]["expanded"] == ["true", "true", "false"]
    assert out["tool"]["counts"] == ["3", "1", "0"]
    assert out["tool"]["dotHidden"] is False
    assert out["tool"]["stored"] == "closed"
    assert out["tool"]["items"] == 1
    assert out["agent"]["expanded"] == ["true", "true", "true"]
    assert out["agent"]["counts"] == ["3", "1", "1"]
    assert out["agent"]["items"] == 1
    assert out["reopened"] == {"hidden": False, "dotHidden": True, "layout": "full"}


def test_an_item_opens_in_the_pane_and_marks_its_message(
    desktop: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, _ = desktop
    opened = out["open"]
    assert opened["paneHidden"] is False
    assert opened["tabs"] == ["Meeting notes"]
    assert opened["layout"] == "narrow" and opened["panelHidden"] is False
    assert opened["widthAfter"] < opened["widthBefore"]
    assert opened["marked"] == "12"
    assert out["afterClose"]["layout"] == "full"
    assert out["afterClose"]["widthAfter"] == opened["widthBefore"]
    assert out["resource"]["tabs"] == ["rules.txt"]


def test_rename_checks_then_sends_and_shows_the_new_title(
    desktop: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, patches = desktop
    assert out["blank"] == {"error": "A title is required.", "errorShown": True}
    assert patches == [{"file_id": 101, "title": "Quarterly notes"}]
    assert out["renamed"]["title"] == "Quarterly notes"
    assert out["renamed"]["form"] is False


def test_phone_is_a_full_screen_overlay_that_gives_way_to_the_pane(
    phone: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, _ = phone
    assert out["errors"] == []
    on_phone = out["phone"]
    assert on_phone["position"] == "fixed"
    assert on_phone["width"] == on_phone["viewport"]
    assert (on_phone["role"], on_phone["modal"]) == ("dialog", "true")
    assert on_phone["withPane"] == {"layout": "aside", "hidden": True}
    assert on_phone["afterPane"] == {"layout": "full", "hidden": False}
    assert on_phone["escape"] == {"hidden": True}


def test_the_panel_works_when_storage_throws(
    blocked: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    out, patches = blocked
    assert out["errors"] == []
    assert out["initial"]["expanded"] == ["true", "false", "false"]
    assert out["initial"]["hidden"] is False
    assert out["toggle"]["expanded"] == ["true", "true", "false"]
    assert out["toggle"]["stored"] == "blocked"
    assert out["tool"]["expanded"] == ["true", "true", "false"]
    assert patches == [{"file_id": 101, "title": "Quarterly notes"}]
