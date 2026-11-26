"""The served chat page in a real browser: start-up, the panel overlay and the pane header.

A headless Chromium opens the real ``chat.html`` as the real application renders it
(personal mode, SQLite) for a conversation that holds a reply and a chart. A thin server
stands between the browser and the application's test client, so every page, script,
style and API answer is the application's own; it adds only a small script to the page
that records page errors and, once the page has settled, reports what it finds.

* The start-up runs to its end: no page error, the history drawn, the chart card drawn
  from the chart API, and the right-hand panel listing the chart (question 94: a stale
  ``configureMarked()`` call threw on DOMContentLoaded and nothing after it ran).
* At phone width (390 by 844) the panel's full-screen overlay fits the screen: nothing
  scrolls sideways, and the close button lies inside the window and takes the keyboard
  (question 95).
* The preview pane's header for a chart keeps the file line clear of the toolbar, in
  light and dark and at the narrowest pane width (question 95).

The page reports on the real clock, so a loaded machine only waits longer. Without a
Chromium the tests are skipped, as the other browser tests are.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversations as convdb
from spark.database import messages as msgdb
from spark.mode import Mode
from spark.tools.registry import execute_builtin_tool
from spark.web.server import attach_database, create_app
from tests.test_preview_renderers import BROWSER, stop_browser
from tests.web_helpers import sign_in

USER = "user-ada-0001"
RUN_BUDGET_S = 240
SALES = {
    "type": "bar",
    "data": {"columns": ["Quarter", "Sales"], "rows": [["Q1", 10], ["Q2", 14]]},
    "encoding": {"x": "Quarter", "y": "Sales"},
}

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(2 * RUN_BUDGET_S + 60),
]


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


INSTANCE_SETTINGS: dict[str, object] = {"interface.session_timeout_minutes": 60}


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return INSTANCE_SETTINGS.get(key, default)


# What the page does once it has loaded, in the order a person would: look at the history,
# open the panel, then open the chart in the preview pane. Each step waits on the real
# clock for what it expects and records what it found, so a failure names what was missing.
PROBE = r"""
<script>
(function () {
  const MODE = '__MODE__';
  const out = { errors: [], mode: MODE };
  window.addEventListener('error', e => out.errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', e => out.errors.push(String(e.reason)));
  function wait(check, ms) {
    return new Promise(resolve => {
      const start = Date.now();
      (function poll() {
        let value = null;
        try { value = check(); } catch (err) { value = null; }
        if (value || Date.now() - start > ms) return resolve(value);
        setTimeout(poll, 50);
      })();
    });
  }
  const box = el => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom,
             width: r.width, height: r.height };
  };
  const meets = (a, b) => !!(a && b) && a.left < b.right && b.left < a.right &&
    a.top < b.bottom && b.top < a.bottom;
  function toolbarClash() {
    const panel = [...document.querySelectorAll('.preview-panel')].find(p => !p.hidden);
    if (!panel) return null;
    const text = panel.querySelector('.preview-toolbar-text');
    const parts = [...text.children].map(box);
    const controls = [...panel.querySelectorAll('.preview-toolbar .chart-action, ' +
      '.preview-toolbar .chart-match, .preview-toolbar .preview-close')].map(el => ({
      name: el.dataset.action || el.className, box: box(el) }));
    const pane = box(document.getElementById('preview-pane'));
    return { text: box(text), parts, controls, pane,
      overlaps: controls.filter(c => c.box && c.box.width > 0 &&
        (parts.some(p => meets(p, c.box)) || meets(box(text), c.box))).map(c => c.name),
      outside: controls.filter(c => c.box && c.box.width > 0 &&
        (c.box.right > pane.right + 0.5 || c.box.left < pane.left - 0.5)).map(c => c.name) };
  }
  async function run() {
    try {
      out.history = !!(await wait(() =>
        document.getElementById('chat-messages').textContent.includes('Here is your chart.'), 30000));
      out.cardDrawn = !!(await wait(() => document.querySelector(
        '.chart-card .chart-card-canvas[data-drawn]'), 30000));
      if (document.getElementById('tool-panel').hidden) document.getElementById('btn-tool-panel').click();
      const item = await wait(() => document.querySelector('#tool-panel .sidecar-item'), 15000);
      const panel = document.getElementById('tool-panel');
      out.panel = { open: !panel.hidden, items: document.querySelectorAll(
        '#tool-panel .sidecar-item').length, itemTitle: item ? item.textContent : '' };
      if (MODE === 'phone') {
        const sections = panel.querySelector('.sidecar-sections');
        const close = document.getElementById('sidecar-close');
        close.focus();
        const closeBox = box(close);
        out.overlay = {
          viewport: { width: window.innerWidth, height: window.innerHeight },
          panel: { scroll: panel.scrollWidth, client: panel.clientWidth, box: box(panel) },
          sections: { scroll: sections.scrollWidth, client: sections.clientWidth },
          page: { scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth },
          close: closeBox,
          closeFocused: document.activeElement === close,
          closeTabIndex: close.tabIndex,
          pageOffenders: [...document.querySelectorAll('body *')]
            .filter(e => box(e).right > document.documentElement.clientWidth + 0.5 && !panel.contains(e))
            .slice(0, 12).map(e => e.tagName + '#' + e.id + '.' + e.className + ' ' + Math.round(box(e).right)),
          offenders: [...panel.querySelectorAll('*')].filter(e => box(e).right > window.innerWidth + 0.5)
            .slice(0, 8).map(e => e.tagName + '.' + e.className + ' ' + Math.round(box(e).right)),
          widest: Math.max(...[...panel.querySelectorAll('.sidecar-item')].map(i => box(i).right)),
        };
      } else {
        document.getElementById('sidecar-close').click();
        document.querySelector('.chart-card .chart-action[data-action="open"]').click();
        await wait(() => document.querySelector('.preview-panel[data-rendered]'), 60000);
        const pane = document.getElementById('preview-pane');
        out.header = { wide: toolbarClash() };
        pane.style.setProperty('--preview-width', '280px');
        await wait(() => box(pane).width <= 281, 5000);
        out.header.narrow = toolbarClash();
      }
    } catch (err) {
      out.errors.push(String(err && err.stack || err));
    }
    out.theme = document.documentElement.getAttribute('data-bs-theme');
    await fetch('/__report', { method: 'POST', body: JSON.stringify(out) });
  }
  window.addEventListener('load', () => setTimeout(run, 0));
})();
</script>
"""


class ServedChat:
    """The real application, signed in, behind a small server a browser can open."""

    def __init__(self, client: TestClient, conversation: int, theme: str, mode: str) -> None:
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        lock = threading.Lock()
        # The saved theme wins over the browser's copy (ui.js), so the settings answer it.
        INSTANCE_SETTINGS["interface.theme"] = theme
        served = self
        probe = PROBE.replace("__MODE__", mode)
        head = f"<script>try{{localStorage.setItem('app-theme','{theme}')}}catch(e){{}}</script>"
        self.url = f"/chat/{conversation}"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/__report":
                    served.report = json.loads(body)
                    served.reported.set()
                self._send(204, b"", "text/plain")

            def do_GET(self) -> None:  # noqa: N802
                with lock:
                    answer = client.get(self.path, follow_redirects=False)
                kind = answer.headers.get("content-type", "application/octet-stream")
                body = answer.content
                if self.path.split("?")[0] == served.url and kind.startswith("text/html"):
                    text = body.decode("utf-8")
                    text = text.replace("<head>", "<head>" + head, 1)
                    text = text.replace("</body>", probe + "</body>", 1)
                    body = text.encode("utf-8")
                self._send(answer.status_code, body, kind)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def chat_client(
    tmp_path_factory: pytest.TempPathFactory, _sqlite_template: Path
) -> Iterator[tuple[TestClient, int]]:
    """The application in personal mode with one conversation: a reply and a chart."""
    import shutil

    from spark.core.conversation_manager import ConversationManager
    from spark.mode import DatabaseSettings, Dialect

    root = tmp_path_factory.mktemp("chat-page")
    path = root / "spark.db"
    shutil.copy(_sqlite_template, path)
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
    files_pkg.configure(
        _Settings({"files.root": str(root / "volume")}), data_dir=root, runtime_mode=Mode.PERSONAL
    )
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    ctx.data_dir = root
    app = create_app(ctx, first_run=False)
    attach_database(app, settings, True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    try:
        with TestClient(app, backend_options=options) as client:
            app.state.user_guid = USER
            sign_in(client)
            db = app.state.sync_db
            app.state.conversation_manager = ConversationManager(
                db, MagicMock(), user_guid=USER, embedded_tools_config={"embedded_tools": {}}
            )
            cid: int = db.call(convdb.create_conversation, "Quarterly sales", "stub/model", USER)
            config = {
                "embedded_tools": {},
                "_user_guid": USER,
                "_files_db": db,
                "_kg_conversation_id": cid,
            }
            text, error = execute_builtin_tool(
                "create_chart", {"title": "Units by quarter", "definition": SALES}, config
            )
            assert error is False, text
            blocks = [
                {"type": "text", "text": "Here is your chart."},
                {"type": "tool_use", "id": "u1", "name": "create_chart", "input": {}},
            ]
            results = [{"type": "tool_result", "tool_use_id": "u1", "content": text}]
            db.call(msgdb.add_message, cid, "user", "Chart our sales.", 5, USER)
            db.call(msgdb.add_message, cid, "assistant", json.dumps(blocks), 5, USER)
            db.call(msgdb.add_message, cid, "user", "[TOOL_RESULTS]" + json.dumps(results), 5, USER)
            yield client, cid
    finally:
        files_pkg.reset()
        shutil.rmtree(root, ignore_errors=True)


def open_page(client: TestClient, cid: int, theme: str, mode: str, size: str) -> dict[str, Any]:
    assert BROWSER is not None
    served = ServedChat(client, cid, theme, mode)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-chat-", ignore_cleanup_errors=True
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
                    f"http://127.0.0.1:{served.port}{served.url}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = served.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, f"the chat page did not report in {RUN_BUDGET_S} s"
        return served.report
    finally:
        served.close()


@pytest.fixture(scope="module")
def desktop_dark(chat_client: tuple[TestClient, int]) -> dict[str, Any]:
    return open_page(*chat_client, "dark", "desktop", "1400,1000")


@pytest.fixture(scope="module")
def desktop_light(chat_client: tuple[TestClient, int]) -> dict[str, Any]:
    return open_page(*chat_client, "light", "desktop", "1400,1000")


@pytest.fixture(scope="module")
def phone(chat_client: tuple[TestClient, int]) -> dict[str, Any]:
    return open_page(*chat_client, "light", "phone", "390,844")


@pytest.mark.parametrize("which", ["desktop_dark", "desktop_light", "phone"])
def test_the_start_up_runs_to_its_end(which: str, request: pytest.FixtureRequest) -> None:
    report = request.getfixturevalue(which)
    assert report["errors"] == []
    assert report["theme"] == ("dark" if which == "desktop_dark" else "light")
    assert report["history"] is True
    assert report["cardDrawn"] is True
    assert report["panel"]["open"] is True
    assert report["panel"]["items"] == 1
    assert "Units by quarter" in report["panel"]["itemTitle"]


def test_the_phone_overlay_fits_the_screen(phone: dict[str, Any]) -> None:
    overlay = phone["overlay"]
    width = overlay["viewport"]["width"]
    assert width <= 390
    assert overlay["panel"]["scroll"] == overlay["panel"]["client"]
    assert overlay["sections"]["scroll"] == overlay["sections"]["client"]
    assert overlay["page"]["scroll"] <= overlay["page"]["client"]
    assert overlay["offenders"] == []
    assert overlay["widest"] <= width
    close = overlay["close"]
    assert 0 <= close["left"] and close["right"] <= width
    assert 0 <= close["top"] and close["bottom"] <= overlay["viewport"]["height"]
    assert overlay["closeFocused"] is True and overlay["closeTabIndex"] >= 0


@pytest.mark.parametrize("which", ["desktop_dark", "desktop_light"])
@pytest.mark.parametrize("width", ["wide", "narrow"])
def test_the_chart_pane_header_has_no_overlap(
    which: str, width: str, request: pytest.FixtureRequest
) -> None:
    report = request.getfixturevalue(which)
    found = report["header"][width]
    assert found is not None
    assert found["controls"], "the chart toolbar was not drawn"
    assert found["text"]["width"] >= 96, "the file line has no room"
    assert all(part["width"] > 0 for part in found["parts"])
    assert found["overlaps"] == []
    assert found["outside"] == []
