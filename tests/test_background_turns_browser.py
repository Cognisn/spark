"""A page that comes back to the user's own running turn, in a real browser on the real clock.

A headless Chromium opens the real ``chat.html`` as the application renders it (personal
mode, SQLite). A thin server stands between the browser and the application's test client:
every page, script, style and API answer is the application's own, and only the
conversation's live view (``GET /stream/conversation``) is scripted, as the server sends it
for a turn of the user's own that another page started:

* the page attaches: the working status shows what the turn is doing, the reply so far
  appears and grows, the composer waits with its reason and the send button is Stop;
* a held tool permission prompt is offered; answering it sends the turn's stream and
  conversation with the answer, and the dialog closes when the answer is confirmed;
* Stop sends the turn's stream and conversation to ``/stream/cancel``; when the turn ends
  the working status goes, the history reloads, and the composer and Send come back.

The page reports on the real clock, so a loaded machine only waits longer. Without a
Chromium the tests are skipped, as the other browser tests are.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from tests.test_chat_page_browser import USER, chat_client  # noqa: F401  (a module fixture)
from tests.test_preview_renderers import BROWSER, stop_browser

RUN_BUDGET_S = 180

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(2 * RUN_BUDGET_S + 60),
]

STREAM = "s-attached"

PROBE = r"""
<script>
(function () {
  const out = { errors: [], steps: {} };
  window.addEventListener('error', e => {
    if (!String(e.message).startsWith('ResizeObserver loop')) out.errors.push(String(e.message));
  });
  window.addEventListener('unhandledrejection', e => out.errors.push(String(e.reason)));
  const wait = (check, ms) => new Promise(resolve => {
    const start = Date.now();
    (function poll() {
      let value = null;
      try { value = check(); } catch (err) { value = null; }
      if (value || Date.now() - start > ms) return resolve(value);
      setTimeout(poll, 25);
    })();
  });
  const modalOpen = id => {
    const el = document.getElementById(id);
    return !!el && el.classList.contains('show');
  };
  function look() {
    const status = document.querySelector('.working-status');
    const input = document.getElementById('chat-input');
    const btn = document.getElementById('btn-send');
    return {
      working: !!status && !status.hidden,
      step: status ? status.querySelector('.working-step').textContent : '',
      text: document.getElementById('chat-messages').innerText,
      inputDisabled: input.disabled,
      placeholder: input.placeholder,
      button: btn.getAttribute('aria-label'),
      buttonDisabled: btn.disabled,
      permission: modalOpen('permissionModal'),
      permissionText: document.getElementById('permission-message').textContent,
      attached: !!(window.SparkTurnAttach && SparkTurnAttach.attached()),
    };
  }
  async function run() {
    try {
      await wait(() => window.SparkTurnAttach && SparkTurnAttach.attached(), 30000);
      await wait(() => look().working && look().step, 15000);
      out.steps.attached = look();
      await fetch('/__seen', { method: 'POST', body: '{}' });
      await wait(() => look().text.includes('Half a reply so far'), 30000);
      out.steps.text = look();
      await wait(() => modalOpen('permissionModal'), 30000);
      out.steps.prompt = look();
      document.querySelector('#permissionModal [onclick="respondPermission(\'once\')"]').click();
      await wait(() => !modalOpen('permissionModal'), 15000);
      out.steps.answered = look();
      await wait(() => look().text.includes('and then some more'), 30000);
      out.steps.more = look();
      document.getElementById('btn-send').click();
      await wait(() => !SparkTurnAttach.attached(), 30000);
      await new Promise(r => setTimeout(r, 800));
      out.steps.finished = look();
    } catch (err) {
      out.errors.push(String(err && err.stack || err));
    }
    await fetch('/__report', { method: 'POST', body: JSON.stringify(out) });
  }
  window.addEventListener('load', () => setTimeout(run, 0));
})();
</script>
"""


class ServedAttach:
    """The real application, with the conversation's live view scripted for a running turn."""

    def __init__(self, client: TestClient, conversation: int) -> None:
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        self.answers: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []
        self.history_loads = 0
        self.seen_working = threading.Event()
        self.answered = threading.Event()
        self.cancelled = threading.Event()
        self.closing = threading.Event()
        lock = threading.Lock()
        served = self
        self.url = f"/chat/{conversation}"

        def frame(event: str, data: dict[str, Any]) -> bytes:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _live(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                turn = {"user_guid": USER, "name": "You", "stream_id": STREAM, "started_at": ""}
                steps: list[Any] = [
                    b"retry: 600000\n\n",
                    frame("ready", {"turn": turn, "me": USER}),
                    frame(
                        "turn_state",
                        {
                            "stream_id": STREAM,
                            "started_at": "",
                            "activity": {"kind": "tool", "tool_name": "web_search"},
                            "agents": [{"agent_id": "a1", "agent_name": "Researcher"}],
                            "prompts": [],
                        },
                    ),
                    frame("turn_text", {"stream_id": STREAM, "text": ""}),
                    served.seen_working,
                    frame(
                        "tool",
                        {"phase": "end", "tool_name": "web_search", "stream_id": STREAM},
                    ),
                    frame("delta", {"seq": 1, "text": "Half a reply so far", "stream_id": STREAM}),
                    1.5,
                    frame(
                        "prompt",
                        {
                            "kind": "permission",
                            "request_id": "p1",
                            "stream_id": STREAM,
                            "tool_name": "web_fetch",
                            "params": {"url": "https://example.com"},
                        },
                    ),
                    served.answered,
                    frame("prompt_closed", {"request_id": "p1", "stream_id": STREAM}),
                    0.5,
                    frame("delta", {"seq": 2, "text": ", and then some more", "stream_id": STREAM}),
                    served.cancelled,
                    frame(
                        "turn_finished",
                        {"status": "cancelled", "user_guid": USER, "stream_id": STREAM},
                    ),
                ]
                try:
                    for step in steps:
                        if isinstance(step, float):
                            time.sleep(step)
                        elif isinstance(step, threading.Event):
                            while not step.wait(0.25):
                                if served.closing.is_set():
                                    return
                                self.wfile.write(b": waiting\n\n")
                                self.wfile.flush()
                        else:
                            self.wfile.write(step)
                            self.wfile.flush()
                    while not served.closing.wait(0.5):
                        self.wfile.write(b": quiet\n\n")
                        self.wfile.flush()
                except OSError:
                    return None

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = urlparse(self.path).path
                if path == "/__report":
                    served.report = json.loads(body)
                    served.reported.set()
                    self._send(204, b"", "text/plain")
                    return
                if path == "/__seen":
                    served.seen_working.set()
                    self._send(204, b"", "text/plain")
                    return
                if path == "/chat/permission/respond":
                    served.answers.append(json.loads(body))
                    served.answered.set()
                    self._send(200, b'{"status": "ok"}', "application/json")
                    return
                if path == "/stream/cancel":
                    served.cancels.append(json.loads(body))
                    served.cancelled.set()
                    self._send(200, b'{"status": "ok"}', "application/json")
                    return
                self._send(404, b"{}", "application/json")

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/stream/conversation":
                    self._live()
                    return
                if parsed.path.endswith("/api/history"):
                    served.history_loads += 1
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
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def run(chat_client: tuple[TestClient, int]) -> tuple[dict[str, Any], ServedAttach]:  # noqa: F811
    client, cid = chat_client
    assert BROWSER is not None
    served = ServedAttach(client, cid)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-attach-", ignore_cleanup_errors=True
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
                    f"http://127.0.0.1:{served.port}{served.url}",
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
        return served.report["steps"], served
    finally:
        served.close()


def test_a_returning_page_attaches_to_its_own_running_turn(run: Any) -> None:
    steps, _ = run
    attached = steps["attached"]
    assert attached["attached"] is True
    # What the turn is doing when the page comes back: its tool, by its friendly name
    assert attached["working"] is True and "web" in attached["step"].lower()
    assert "Half a reply so far" in steps["text"]["text"]
    assert attached["inputDisabled"] is True
    assert (
        attached["placeholder"] == "Spark is still answering your message. Stop it to send another."
    )
    assert attached["button"] == "Stop the response" and attached["buttonDisabled"] is False


def test_a_held_prompt_is_offered_and_answered_for_its_turn(run: Any) -> None:
    steps, served = run
    assert steps["prompt"]["permission"] is True
    assert '"web_fetch"' in steps["prompt"]["permissionText"]
    assert served.answers[0]["request_id"] == "p1"
    assert served.answers[0]["decision"] == "once"
    assert served.answers[0]["stream_id"] == STREAM
    assert int(served.answers[0]["conversation_id"]) > 0
    assert steps["answered"]["permission"] is False
    assert "and then some more" in steps["more"]["text"]


def test_stop_cancels_the_attached_turn_and_the_end_settles_the_page(run: Any) -> None:
    steps, served = run
    assert served.cancels and served.cancels[0]["stream_id"] == STREAM
    assert int(served.cancels[0]["conversation_id"]) > 0
    finished = steps["finished"]
    assert finished["attached"] is False and finished["working"] is False
    assert finished["inputDisabled"] is False
    assert finished["button"] == "Send"
    # Loaded when the page opened, and again when the turn ended
    assert served.history_loads >= 2
