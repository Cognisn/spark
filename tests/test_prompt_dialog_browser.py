"""The tool permission and agent model approval dialogs close however quickly they are answered.

A headless Chromium opens the real ``chat.html`` as the application renders it. Bootstrap
ignores ``Modal.hide()`` while the dialog's fade-in runs, so a dialog answered (or closed
from another page) in its first moments used to stay open. Each scenario shows a dialog and
answers it, or closes it as another page's answer would, in the same tick, then reports
whether the dialog is still open once the transitions are long over. The answers' network
calls are stubbed in the page, so the timing is the page's own and the real clock decides
when the fade ends.

Without a Chromium the tests are skipped, as the other browser tests are.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from tests.test_chat_page_browser import chat_client  # noqa: F401  (a module fixture)
from tests.test_preview_renderers import BROWSER, stop_browser

RUN_BUDGET_S = 90

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(RUN_BUDGET_S + 60),
]

PROBE = r"""
<script>
(function () {
  const out = { errors: [], open: null, backdrops: null, scenario: location.hash.slice(1) };
  window.addEventListener('error', e => {
    if (!String(e.message).startsWith('ResizeObserver loop')) out.errors.push(String(e.message));
  });
  const realFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => /\/chat\/(permission\/respond|agent\/model-approve)/.test(String(url))
    ? Promise.resolve(new Response('{"status":"ok"}', { status: 200 }))
    : realFetch(url, opts);
  const isOpen = id => document.getElementById(id).classList.contains('show');
  const tick = ms => new Promise(r => setTimeout(r, ms));
  const permission = { request_id: 'p1', tool_name: 'web_fetch', params: { url: 'https://example.com' },
                       stream_id: 's1' };
  const agent = { request_id: 'm1', agent_name: 'Researcher', task: 'Look it up',
                  available_models: [{ id: 'stub/model', name: 'Stub', context_length: 8000 }],
                  suggested_model: 'stub/model', stream_id: 's1' };
  async function run() {
    const scenario = out.scenario;
    let id = 'permissionModal';
    try {
      if (scenario === 'permission-answered') {
        showPermissionPrompt(permission);
        respondPermission('once');
      } else if (scenario === 'permission-closed') {
        showPermissionPrompt(permission);
        closePrompt('p1');
      } else if (scenario === 'agent-answered') {
        id = 'agentModelModal';
        showAgentModelApproval(agent);
        approveAgentModel();
      } else if (scenario === 'agent-closed') {
        id = 'agentModelModal';
        showAgentModelApproval(agent);
        closePrompt('m1');
      } else if (scenario === 'permission-reshown') {
        // Closed during the fade-in, then asked again once it has closed: it opens
        showPermissionPrompt(permission);
        closePrompt('p1');
        await tick(1500);
        showPermissionPrompt({ ...permission, request_id: 'p2' });
        await tick(1500);
        out.reopened = isOpen('permissionModal');
        closePrompt('p2');
      } else if (scenario === 'permission-asked-while-closing') {
        // Asked again while the previous prompt's dialog is fading out: it opens
        showPermissionPrompt(permission);
        await tick(1500);
        closePrompt('p1');
        showPermissionPrompt({ ...permission, request_id: 'p2' });
        await tick(1500);
        out.reopened = isOpen('permissionModal');
        closePrompt('p2');
      }
      await tick(1500);
      out.open = isOpen(id);
      out.backdrops = document.querySelectorAll('.modal-backdrop').length;
      out.bodyOpen = document.body.classList.contains('modal-open');
    } catch (err) {
      out.errors.push(String(err && err.stack || err));
    }
    await realFetch('/__report', { method: 'POST', body: JSON.stringify(out) });
  }
  window.addEventListener('load', () => setTimeout(run, 500));
})();
</script>
"""


class Served:
    """The real application, with a probe injected into the chat page."""

    def __init__(self, client: TestClient, conversation: int) -> None:
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        lock = threading.Lock()
        served = self
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
                if urlparse(self.path).path == "/__report":
                    served.report = json.loads(body)
                    served.reported.set()
                    self._send(204, b"", "text/plain")
                    return
                self._send(404, b"{}", "application/json")

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.startswith("/stream/"):
                    self._send(404, b"{}", "application/json")
                    return
                with lock:
                    answer = client.get(self.path, follow_redirects=False)
                kind = answer.headers.get("content-type", "application/octet-stream")
                body = answer.content
                if parsed.path == served.url and kind.startswith("text/html"):
                    text = body.decode("utf-8").replace("</body>", PROBE + "</body>", 1)
                    body = text.encode("utf-8")
                self._send(answer.status_code, body, kind)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _run(client: TestClient, cid: int, scenario: str) -> dict[str, Any]:
    assert BROWSER is not None
    served = Served(client, cid)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-dialog-", ignore_cleanup_errors=True
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
                    f"http://127.0.0.1:{served.port}{served.url}#{scenario}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = served.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, f"the page did not report in {RUN_BUDGET_S} s"
        assert served.report["errors"] == []
        return served.report
    finally:
        served.close()


@pytest.mark.parametrize(
    "scenario",
    ["permission-answered", "permission-closed", "agent-answered", "agent-closed"],
)
def test_a_dialog_answered_or_closed_while_it_fades_in_ends_closed(
    chat_client: tuple[TestClient, int],  # noqa: F811
    scenario: str,
) -> None:
    client, cid = chat_client
    report = _run(client, cid, scenario)
    assert report["open"] is False
    assert report["backdrops"] == 0
    assert report["bodyOpen"] is False


@pytest.mark.parametrize("scenario", ["permission-reshown", "permission-asked-while-closing"])
def test_a_dialog_closed_early_opens_again_for_the_next_prompt(
    chat_client: tuple[TestClient, int],  # noqa: F811
    scenario: str,
) -> None:
    client, cid = chat_client
    report = _run(client, cid, scenario)
    assert report["reopened"] is True
    assert report["open"] is False
    assert report["backdrops"] == 0
