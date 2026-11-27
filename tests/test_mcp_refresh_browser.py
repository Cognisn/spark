"""The Refresh tools control on the MCP servers page, in a real browser.

A headless Chromium opens the real settings page as the application renders it
(personal mode, SQLite, a stub MCP client whose tools change). A thin server stands
between the browser and the application's test client; a small script added to the
page focuses the control with the keyboard, presses it, and reports what the page
then shows, in light and dark. Without a Chromium the tests are skipped.
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
import yaml
from fastapi.testclient import TestClient

from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from spark.mode import DatabaseSettings, Dialect
from spark.web.server import attach_database, create_app
from tests.test_mcp_refresh_manager import StubClient, _tool
from tests.test_preview_renderers import BROWSER, stop_browser
from tests.web_helpers import sign_in

RUN_BUDGET_S = 180

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(2 * RUN_BUDGET_S + 60),
]

INSTANCE_SETTINGS: dict[str, object] = {"interface.session_timeout_minutes": 60}


def _settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return INSTANCE_SETTINGS.get(key, default)


PROBE = r"""
<script>
(function () {
  const out = { errors: [] };
  window.addEventListener('error', e => {
    if (!String(e.message).startsWith('ResizeObserver loop')) out.errors.push(String(e.message));
  });
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
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.right <= window.innerWidth &&
      r.bottom <= window.innerHeight;
  }
  async function run() {
    try {
      const nav = document.querySelector('a[href="#section-mcp-servers"]');
      if (nav) nav.click();
      const button = await wait(() => document.getElementById('mcp-refresh-docs'), 60000);
      out.found = !!button;
      if (button) {
        out.label = button.getAttribute('aria-label');
        out.visible = visible(button);
        out.tabIndex = button.tabIndex;
        button.focus();
        out.focused = document.activeElement === button;
        button.click();
        const note = await wait(() => document.querySelector('.mcp-refresh-note'), 60000);
        out.note = note ? note.textContent : null;
        out.noteColour = note ? getComputedStyle(note).color : null;
        out.background = getComputedStyle(document.body).backgroundColor;
        out.refocused = !!(await wait(
          () => document.activeElement && document.activeElement.id === 'mcp-refresh-docs', 5000));
        const rows = [...document.querySelectorAll('#mcp-server-list > div')];
        const wiki = rows.find(r => r.textContent.includes('wiki'));
        out.wiki = wiki ? wiki.textContent.replace(/\s+/g, ' ') : null;
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


class Served:
    def __init__(self, client: TestClient, theme: str) -> None:
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        lock = threading.Lock()
        served = self
        INSTANCE_SETTINGS["interface.theme"] = theme
        head = f"<script>try{{localStorage.setItem('app-theme','{theme}')}}catch(e){{}}</script>"

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
                    return
                with lock:
                    answer = client.post(self.path, content=body)
                self._send(
                    answer.status_code,
                    answer.content,
                    answer.headers.get("content-type", "application/json"),
                )

            def do_GET(self) -> None:  # noqa: N802
                with lock:
                    answer = client.get(self.path, follow_redirects=False)
                kind = answer.headers.get("content-type", "application/octet-stream")
                body = answer.content
                if self.path.split("?")[0] == "/settings" and kind.startswith("text/html"):
                    text = body.decode("utf-8")
                    text = text.replace("<head>", "<head>" + head, 1)
                    text = text.replace("</body>", PROBE + "</body>", 1)
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
def settings_client(
    tmp_path_factory: pytest.TempPathFactory, _sqlite_template: Path
) -> Iterator[TestClient]:
    import shutil

    import spark.core.application as app_mod

    root = tmp_path_factory.mktemp("mcp-refresh")
    path = root / "spark.db"
    shutil.copy(_sqlite_template, path)
    config = root / "config.yaml"
    servers = [
        {"name": n, "transport": "http", "url": f"https://{n}.example.com/mcp"}
        for n in ("docs", "wiki")
    ]
    config.write_text(yaml.safe_dump({"mcp": {"servers": servers}}), encoding="utf-8", newline="\n")
    patch = pytest.MonkeyPatch()
    patch.setattr(app_mod, "_get_config_path", lambda: config)
    ctx = MagicMock()
    ctx.settings.get = _settings_get
    ctx.data_dir = root
    app = create_app(ctx, first_run=False)
    attach_database(app, DatabaseSettings(Dialect.SQLITE, sqlite_path=path), True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    try:
        with TestClient(app, backend_options=options) as client:
            sign_in(client)
            yield client
    finally:
        patch.undo()
        shutil.rmtree(root, ignore_errors=True)


def open_page(client: TestClient, theme: str) -> dict[str, Any]:
    assert BROWSER is not None
    StubClient.registry = {}
    manager = MCPManager(client_factory=StubClient)
    for name in ("docs", "wiki"):
        manager.add_server(
            MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
        )

    async def start() -> None:
        await manager.connect_all()
        await manager.list_all_tools()

    asyncio.run(start())
    StubClient.registry["docs"].tools = [_tool("docs", "beta"), _tool("docs", "gamma")]
    client.app.state.mcp_manager = manager  # type: ignore[attr-defined]
    served = Served(client, theme)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-mcp-", ignore_cleanup_errors=True
        ) as profile:
            browser = subprocess.Popen(
                [
                    BROWSER,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1400,1000",
                    f"--user-data-dir={profile}",
                    f"http://127.0.0.1:{served.port}/settings",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = served.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, f"the settings page did not report in {RUN_BUDGET_S} s"
        return served.report
    finally:
        served.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_refresh_control_works_by_keyboard_and_shows_what_changed(
    settings_client: TestClient, theme: str
) -> None:
    report = open_page(settings_client, theme)
    assert report["errors"] == []
    assert report["theme"] == theme
    assert report["found"] is True
    assert "docs" in report["label"]
    assert report["tabIndex"] >= 0
    assert report["visible"] is True
    assert report["focused"] is True
    assert report["note"] is not None
    assert "Added: beta, gamma" in report["note"]
    assert "Removed: alpha" in report["note"]
    assert "2 tools" in report["note"]
    assert report["refocused"] is True
    assert report["noteColour"] != report["background"]
    assert "1 tools" in report["wiki"]
