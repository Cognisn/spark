"""Each renderer, with a fixture, in a real browser (CS38).

A headless Chromium opens a harness page that holds the real preview pane
(``_preview_pane.html``, ``preview.js``, ``preview.css``) and serves the real frame
(``preview_frame.html``, ``preview_frame.js`` and the vendored libraries under
``static/vendor``) with the frame's own Content Security Policy
(:data:`spark.web.endpoints.preview.FRAME_POLICY`). For each synthetic document the
harness opens its card, waits for the sandboxed frame to report what it drew, and
writes the results into the page, which the test reads back:

* PDF (PDF.js, handed over as text and run on the frame's thread), Word (docx-preview),
  Excel, legacy Excel and CSV (SheetJS, with sheet tabs), PNG and SVG (an image; the
  SVG's script never runs), Markdown (marked and DOMPurify; the injected handler is
  removed), text and HTML (as text with line numbers), and anything else (details).

The browser is the Chromium Playwright installs, or the one ``SPARK_TEST_CHROMIUM``
names; without one the test is skipped, as the Node tests are without Node.

The harness runs on the real clock: the page posts its results back to the harness server
when it has finished, and the browser is stopped then. Each card gets a generous budget
(``CARD_BUDGET_MS``) so a loaded machine, such as the full suite running beside a
browser, can finish PDF.js; a frame that reports an error is recorded at once, never
waited out, and the card that ran out of budget is named with how long it waited.
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re
import shutil
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
from tests.preview_helpers import (
    csv_bytes,
    docx_bytes,
    markdown_bytes,
    pdf_bytes,
    png_bytes,
    svg_bytes,
    text_bytes,
    xlsx_bytes,
    zip_bytes,
)

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"
SHEETJS = STATIC / "vendor" / "sheetjs" / "xlsx.full.min.js"


def chromium() -> str | None:
    named = os.environ.get("SPARK_TEST_CHROMIUM")
    if named and Path(named).is_file():
        return named
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not base.is_dir():
        base = Path.home() / ".cache" / "ms-playwright"
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*",
        "chromium-*/chrome-*/chrome.exe",
        "chromium-*/chrome-linux/chrome",
    )
    for pattern in patterns:
        found = sorted(p for p in base.glob(pattern) if p.is_file() and p.suffix in ("", ".exe"))
        if found:
            return str(found[-1])
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


BROWSER = chromium()

# A card has this long, on the real clock, to report; the whole page has RUN_BUDGET_S.
# An error the frame reports ends the wait for that card at once, so only a frame that
# is still working (or has stopped) ever uses the budget.
CARD_BUDGET_MS = 90_000
RUN_BUDGET_S = 400
pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(RUN_BUDGET_S + 60),
]


def xls_bytes() -> bytes | None:
    """A legacy Excel (BIFF8) workbook, written by SheetJS under Node."""
    node = shutil.which("node")
    if node is None:
        return None
    script = (
        "const X=require(process.argv[1]);const wb=X.utils.book_new();"
        "X.utils.book_append_sheet(wb,X.utils.aoa_to_sheet([['Site','Count'],['Example',3]]),"
        "'Legacy');process.stdout.write(X.write(wb,{bookType:'biff8',type:'base64'}));"
    )
    done = subprocess.run(
        [node, "-e", script, str(SHEETJS)], capture_output=True, text=True, timeout=60
    )
    return base64.b64decode(done.stdout) if done.returncode == 0 else None


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def documents() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = [
        {"name": "report.pdf", "kind": "pdf", "type": "application/pdf", "body": pdf_bytes(2)},
        {"name": "briefing.docx", "kind": "docx", "type": DOCX, "body": docx_bytes()},
        {"name": "budget.xlsx", "kind": "sheet", "type": XLSX, "body": xlsx_bytes()},
        {"name": "hosts.csv", "kind": "sheet", "type": "text/csv", "body": csv_bytes()},
        {"name": "chart.png", "kind": "image", "type": "image/png", "body": png_bytes()},
        {"name": "logo.svg", "kind": "image", "type": "image/svg+xml", "body": svg_bytes()},
        {"name": "notes.md", "kind": "markdown", "type": "text/markdown", "body": markdown_bytes()},
        {"name": "hello.py", "kind": "text", "type": "text/plain", "body": text_bytes()},
        {
            "name": "page.html",
            "kind": "text",
            "type": "text/html",
            "body": b"<h1>x</h1>\n<script>window.parent.postMessage('html script ran','*')</script>\n",
        },
        {"name": "bundle.zip", "kind": "other", "type": "application/zip", "body": zip_bytes()},
    ]
    legacy = xls_bytes()
    if legacy is not None:
        found.append(
            {
                "name": "legacy.xls",
                "kind": "sheet",
                "type": "application/vnd.ms-excel",
                "body": legacy,
            }
        )
    for number, doc in enumerate(found, start=1):
        doc["file_id"] = number
    return found


HARNESS = """<!DOCTYPE html>
<html lang="en" data-bs-theme="light"><head><meta charset="utf-8"><title>harness</title>
<link href="/static/css/cognisn.css" rel="stylesheet">
<link href="/static/css/base.css" rel="stylesheet">
<link href="/static/css/spark.css" rel="stylesheet">
<link rel="stylesheet" href="/static/css/preview.css"></head>
<body><div class="d-flex" style="display:flex;height:900px">
<div style="flex:1"><div id="chat-messages"></div></div>
{pane}
</div>
<pre id="results">pending</pre>
<script src="/static/js/preview.js"></script>
<script>
const cards = {cards};
const only = new URLSearchParams(location.search).get('only');
const stray = [];
window.addEventListener('message', e => {{
  if (typeof e.data === 'string') stray.push(e.data);
}});
function wait(check, ms) {{
  return new Promise(resolve => {{
    const start = Date.now();
    (function poll() {{
      const value = check();
      if (value || Date.now() - start > ms) return resolve(value);
      setTimeout(poll, 50);
    }})();
  }});
}}
const BUDGET = {budget};
(async () => {{
  const results = {{}};
  const timings = {{}};
  for (const card of cards) {{
    if (only && card.name !== only) continue;
    SparkPreview.appendCard(card);
    SparkPreview.open(card, {{ focus: false }});
    const began = Date.now();
    // 'error' is set by the panel when the frame posts spark-preview-error, so a
    // reported failure ends this wait at once; only silence uses the budget.
    const panel = await wait(() => {{
      const p = document.getElementById(`preview-1-${{card.file_id}}-panel`);
      return p && p.dataset.rendered ? p : null;
    }}, BUDGET);
    const waited = Date.now() - began;
    results[card.name] = panel
      ? {{ rendered: panel.dataset.rendered, detail: JSON.parse(panel.dataset.detail || '{{}}') }}
      : {{ rendered: 'timeout', waited_ms: waited }};
    timings[card.name] = waited;
    fetch('/progress', {{ method: 'POST', body: JSON.stringify({{ results, timings }}) }});
  }}
  await new Promise(r => setTimeout(r, 300));
  results.__stray = stray;
  results.__cards = document.querySelectorAll('.document-card').length;
  document.getElementById('results').textContent = JSON.stringify(results);
  await fetch('/report', {{ method: 'POST', body: JSON.stringify({{ results, timings }}) }});
}})();
</script></body></html>"""


CHAT_HARNESS = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>chat</title></head>
<body><div style="display:flex"><div id="chat-messages"></div>{pane}</div>
<div id="tool-panel" style="display:flex"></div>
<pre id="results">pending</pre>
<script src="/static/vendor/marked/marked.umd.js"></script>
<script src="/static/js/main.js"></script>
<script src="/static/js/preview.js"></script>
<script src="/static/js/chat.js"></script>
<script>
function line(id) {{
  return '[spark-preview] ' + JSON.stringify({{file_id: id, conversation_id: 1, name: 'n' + id + '.txt',
    title: 'Doc ' + id, type: 'text/plain', kind: 'text', size: 3}});
}}
function turn(useId, fileId, name) {{
  appendMessage({{ role: 'assistant', content: JSON.stringify([
    {{ type: 'text', text: 'Showing it.' }},
    {{ type: 'tool_use', id: useId, name: name || 'show_document', input: {{ file_id: fileId }} }}]) }});
  appendMessage({{ role: 'user', content: '[TOOL_RESULTS]' + JSON.stringify([
    {{ type: 'tool_result', tool_use_id: useId, content: 'Shown.\\n' + line(fileId) }}]) }});
}}
document.addEventListener('DOMContentLoaded', () => {{
const out = {{}};
// History as first drawn: a card, nothing opened.
SparkPreview.beginHistory(true);
turn('u1', 1);
turn('u2', 2, 'some_mcp_tool');  // another tool's result never becomes a card
SparkPreview.endHistory();
out.initial = {{ cards: document.querySelectorAll('.document-card').length,
  open: !document.getElementById('preview-pane').hidden }};
// A reload after another member's turn: the new document opens.
SparkPreview.beginHistory(false);
document.getElementById('chat-messages').innerHTML = '';
turn('u1', 1);
turn('u3', 3);
SparkPreview.endHistory();
out.reload = {{ cards: document.querySelectorAll('.document-card').length,
  tabs: [...document.querySelectorAll('.preview-tab')].map(t => t.textContent.trim()) }};
// The sender's own turn, live: card and pane at once.
updateStreamingToolComplete('u4', 'show_document', 'Shown.\\n' + line(4), 'success');
updateStreamingToolComplete('u5', 'show_document', 'The document was not shown: no', 'error');
out.live = {{ cards: document.querySelectorAll('.document-card').length,
  tabs: [...document.querySelectorAll('.preview-tab')].map(t => t.textContent.trim()),
  selected: document.querySelector('.preview-tab[aria-selected="true"]').textContent.trim() }};
// The pane's close button closes every tab and the pane.
document.getElementById('preview-pane-close').click();
out.closed = {{ hidden: document.getElementById('preview-pane').hidden,
  tabs: document.querySelectorAll('.preview-tab').length }};
// A card reopens its document.
document.querySelector('.document-card').click();
out.reopened = [...document.querySelectorAll('.preview-tab')].map(t => t.textContent.trim());
document.getElementById('results').textContent = JSON.stringify(out);
}});
</script></body></html>"""


class Harness:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        frame = env.get_template("preview_frame.html").render().encode("utf-8")
        pane = env.get_template("_preview_pane.html").render(conversation={"id": 1})
        cards = [
            {k: d[k] for k in ("file_id", "name", "kind", "type")}
            | {"conversation_id": 1, "title": d["name"], "size": len(d["body"])}
            for d in docs
        ]
        page = HARNESS.format(pane=pane, cards=json.dumps(cards), budget=CARD_BUDGET_MS).encode(
            "utf-8"
        )
        chat_page = CHAT_HARNESS.format(pane=pane).encode("utf-8")
        bodies = {d["file_id"]: d for d in docs}

        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        harness = self

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
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if self.path in ("/progress", "/report"):
                    harness.report = json.loads(body)
                if self.path == "/report":
                    harness.reported.set()
                self._send(204, b"", "text/plain")

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/harness":
                    self._send(200, page, "text/html; charset=utf-8")
                elif path == "/chat-harness":
                    self._send(200, chat_page, "text/html; charset=utf-8")
                elif path == "/preview/frame":
                    self._send(
                        200,
                        frame,
                        "text/html; charset=utf-8",
                        Content_Security_Policy=FRAME_POLICY,
                        X_Content_Type_Options="nosniff",
                    )
                elif match := re.fullmatch(r"/preview/api/1/files/(\d+)", path):
                    doc = bodies.get(int(match.group(1)))
                    if doc is None:
                        self._send(404, b"", "text/plain")
                    else:
                        self._send(200, doc["body"], "application/octet-stream")
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
                    self._send(200, target.read_bytes(), kind, X_Content_Type_Options="nosniff")
                else:
                    self._send(404, b"", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_browser(url: str, *extra: str) -> str:
    assert BROWSER is not None
    done = subprocess.run(
        [
            BROWSER,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1400,900",
            "--virtual-time-budget=60000",
            *extra,
            url,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return done.stdout


def stop_browser(browser: subprocess.Popen[bytes]) -> None:
    """Stop the browser and the helper processes it started (this one only, by its pid)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(browser.pid)],
            capture_output=True,
            check=False,
        )
    else:
        browser.terminate()
    try:
        browser.wait(15)
    except subprocess.TimeoutExpired:
        browser.kill()
        browser.wait()


def run_harness(harness: Harness) -> dict[str, Any]:
    """Open the harness page in a browser on the real clock; stop it once it reports."""
    assert BROWSER is not None
    with tempfile.TemporaryDirectory(
        prefix="spark-preview-", ignore_cleanup_errors=True
    ) as profile:
        browser = subprocess.Popen(
            [
                BROWSER,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1400,900",
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
    assert finished, (
        f"the harness did not finish in {RUN_BUDGET_S} s; "
        f"cards that had reported: {harness.report.get('timings', {})}"
    )
    return harness.report


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    return documents()


@pytest.fixture(scope="module")
def results(docs: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    harness = Harness(docs)
    try:
        report = run_harness(harness)
    finally:
        harness.close()
    if os.environ.get("SPARK_TEST_SHOW_TIMINGS"):
        print("preview card timings (ms):", report["timings"])
    yield report["results"]


def test_the_chat_page_draws_cards_and_opens_the_pane(docs: list[dict[str, Any]]) -> None:
    harness = Harness(docs)
    try:
        dom = run_browser(f"http://127.0.0.1:{harness.port}/chat-harness", "--dump-dom")
    finally:
        harness.close()
    match = re.search(r'<pre id="results">(.*?)</pre>', dom, re.S)
    assert match is not None
    got = json.loads(html.unescape(match.group(1)))
    assert got["initial"] == {"cards": 1, "open": False}
    assert got["reload"] == {"cards": 2, "tabs": ["Doc 3"]}
    assert got["live"] == {"cards": 3, "tabs": ["Doc 3", "Doc 4"], "selected": "Doc 4"}
    assert got["closed"] == {"hidden": True, "tabs": 0}
    assert got["reopened"] == ["Doc 1"]


def test_pdf(results: dict[str, Any]) -> None:
    assert results["report.pdf"] == {"rendered": "pdf", "detail": {"pages": 2, "shown": 2}}


def test_word(results: dict[str, Any]) -> None:
    got = results["briefing.docx"]
    assert got["rendered"] == "docx"
    assert got["detail"]["paragraphs"] >= 2 and got["detail"]["tables"] == 1


def test_excel_with_sheet_tabs(results: dict[str, Any]) -> None:
    got = results["budget.xlsx"]
    assert got["rendered"] == "sheet" and got["detail"]["sheets"] == 2
    assert got["detail"]["cells"] == 8  # the first sheet: 4 rows of 2


def test_legacy_excel(results: dict[str, Any], docs: list[dict[str, Any]]) -> None:
    if not any(d["name"] == "legacy.xls" for d in docs):
        pytest.skip("Node.js is not installed to write the legacy workbook")
    got = results["legacy.xls"]
    assert got["rendered"] == "sheet" and got["detail"] == {"sheets": 1, "cells": 4}


def test_csv(results: dict[str, Any]) -> None:
    assert results["hosts.csv"] == {"rendered": "sheet", "detail": {"sheets": 1, "cells": 6}}


def test_images(results: dict[str, Any]) -> None:
    assert results["chart.png"] == {"rendered": "image", "detail": {"width": 8, "height": 4}}
    assert results["logo.svg"] == {"rendered": "image", "detail": {"width": 40, "height": 20}}


def test_markdown_is_sanitised(results: dict[str, Any]) -> None:
    assert results["notes.md"] == {"rendered": "markdown", "detail": {"headings": 1, "scripts": 0}}


def test_text_and_html_as_text(results: dict[str, Any]) -> None:
    assert results["hello.py"] == {"rendered": "text", "detail": {"lines": 5}}
    assert results["page.html"] == {"rendered": "text", "detail": {"lines": 3}}


def test_anything_else_shows_its_details(results: dict[str, Any]) -> None:
    assert results["bundle.zip"] == {"rendered": "other", "detail": {}}


def test_no_document_ran_a_script(results: dict[str, Any]) -> None:
    assert results["__stray"] == []


def test_every_document_has_its_card(results: dict[str, Any], docs: list[dict[str, Any]]) -> None:
    assert results["__cards"] == len(docs)
