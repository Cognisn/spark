"""The working indicator and its status line, in a real browser on the real clock.

A headless Chromium opens the real ``chat.html``, ``debate.html`` and ``panel.html`` as the
application renders them. A thin server stands between the browser and the application's
test client: every page, script and style is the application's own, and only the event
stream (and the debate and panel state) is scripted, with real pauses between events, so
that each state can be seen on the real clock. A recorder in the page notes every change of
the indicator, and the tests read that trace.

* Chat: sent, reasoning, each tool kind (a friendly name, a file read, an MCP tool, a
  document made), an agent, compaction, the reply starting, a stop, an error, a dropped
  connection, a turn with no text, and the end of the turn.
* The time on a step shows after a few seconds, and is hidden from a screen reader.
* The block is a polite live region, and under reduced motion the dots do not move.
* Tool input and results never reach the page's indicator.
* Debate and panel show who is preparing a turn and which tool they use.

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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from tests.test_chat_page_browser import chat_client  # noqa: F401  (a module fixture)
from tests.test_chat_page_browser import quiet_live_view
from tests.test_preview_renderers import BROWSER, stop_browser

RUN_BUDGET_S = 240
WEB = Path(__file__).parents[1] / "src" / "spark" / "web"

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(2 * RUN_BUDGET_S + 60),
]

PAUSE = 0.7  # between events, so the recorder sees each state

Step = tuple[Any, ...]


def _ev(event: str, /, **data: Any) -> Step:
    return ("event", event, data)


def _sleep(seconds: float) -> Step:
    return ("sleep", seconds)


STATUS = _ev("status", status="processing", stream_id="s1")
DONE = _ev("complete", status="done")

# Each chat scenario is what the stream says, in order; the message names the scenario.
CHAT: dict[str, list[Step]] = {
    "plain": [
        STATUS,
        _sleep(PAUSE),
        _ev("response", content="Hello ", final=False),
        _sleep(PAUSE),
        _ev("response", content="Hello there", final=True),
        _sleep(0.2),
        DONE,
    ],
    "reasoning": [
        STATUS,
        _sleep(PAUSE),
        _ev("reasoning", text="Weighing the options."),
        _sleep(PAUSE),
        _ev("response", content="Decided.", final=True),
        _sleep(0.2),
        DONE,
    ],
    "tools": [
        STATUS,
        _sleep(PAUSE),
        _ev(
            "tool_start", tool_use_id="t1", tool_name="web_search", params={"query": "SECRET-QUERY"}
        ),
        _sleep(PAUSE),
        _ev(
            "tool_complete",
            tool_use_id="t1",
            tool_name="web_search",
            result="SECRET-RESULT",
            status="success",
        ),
        _sleep(PAUSE),
        _ev(
            "tool_start",
            tool_use_id="t2",
            tool_name="read_pdf",
            params={"path": "C:\\private\\docs\\report.pdf"},
        ),
        _sleep(PAUSE),
        _ev(
            "tool_complete",
            tool_use_id="t2",
            tool_name="read_pdf",
            result="SECRET-RESULT",
            status="success",
        ),
        _sleep(PAUSE),
        _ev(
            "tool_start",
            tool_use_id="t3",
            tool_name="srv__lookup_thing",
            params={"q": "SECRET-QUERY"},
        ),
        _sleep(PAUSE),
        _ev(
            "tool_complete",
            tool_use_id="t3",
            tool_name="srv__lookup_thing",
            result="x",
            status="success",
        ),
        _sleep(PAUSE),
        _ev("tool_start", tool_use_id="t4", tool_name="create_chart", params={}),
        _sleep(PAUSE),
        _ev(
            "tool_complete",
            tool_use_id="t4",
            tool_name="create_chart",
            result="x",
            status="success",
        ),
        _sleep(PAUSE),
        _ev(
            "agent_start",
            agent_id="a1",
            agent_name="Researcher",
            task="SECRET-TASK",
            model_id="m",
            mode="x",
        ),
        _sleep(PAUSE),
        _ev(
            "agent_complete",
            agent_id="a1",
            agent_name="Researcher",
            status="success",
            result="done",
        ),
        _sleep(PAUSE),
        _ev("compaction_status", status="start"),
        _sleep(PAUSE),
        _ev(
            "compaction_status",
            status="complete",
            original_tokens=10,
            new_tokens=5,
            messages_rolled_up=2,
        ),
        _sleep(PAUSE),
        _ev("response", content="All done.", final=True),
        _sleep(0.2),
        DONE,
    ],
    "slow": [
        STATUS,
        _ev(
            "tool_start",
            tool_use_id="t1",
            tool_name="web_fetch",
            params={"url": "https://example.com"},
        ),
        _sleep(7.0),
        _ev("tool_complete", tool_use_id="t1", tool_name="web_fetch", result="x", status="success"),
        _sleep(PAUSE),
        _ev("response", content="Fetched.", final=True),
        _sleep(0.2),
        DONE,
    ],
    "toolsonly": [
        STATUS,
        _sleep(PAUSE),
        _ev("tool_start", tool_use_id="t1", tool_name="web_search", params={}),
        _sleep(PAUSE),
        _ev(
            "tool_complete", tool_use_id="t1", tool_name="web_search", result="x", status="success"
        ),
        _sleep(PAUSE),
        DONE,
    ],
    "error": [
        STATUS,
        _sleep(PAUSE),
        _ev("error", message="The model could not be reached."),
    ],
    "stop": [STATUS, _sleep(30.0), DONE],
    "dropped": [],  # scripted by the server: the connection drops after its first event
}

DEBATE: list[Step] = [
    _ev("stream_start", stream_id="d1"),
    _sleep(PAUSE),
    _ev("floor", role="judge"),
    _sleep(PAUSE),
    _ev("judge_text", phase="opening", text="Opening remarks."),
    _ev("floor", role="none"),
    _ev("debater_turn_start", role="pro", round=1),
    _sleep(PAUSE),
    _ev("agent_tool_call", role="pro", tool_name="web_search", params={"query": "SECRET-QUERY"}),
    _sleep(PAUSE),
    _ev(
        "agent_tool_result",
        role="pro",
        tool_name="web_search",
        result="SECRET-RESULT",
        status="success",
    ),
    _sleep(PAUSE),
    _ev("argument", role="pro", round=1, text="My argument."),
    _sleep(0.2),
    _ev("complete"),
]

PANEL: list[Step] = [
    _ev("stream_start", stream_id="p1"),
    _sleep(PAUSE),
    _ev("floor", role="moderator"),
    _sleep(PAUSE),
    _ev("moderator_text", phase="framing", text="Framing."),
    _ev("floor", role="none"),
    _ev("panellist_turn_start", role="panellist:1", name="Ada", round=1),
    _sleep(PAUSE),
    _ev(
        "agent_tool_call",
        role="panellist:1",
        tool_name="web_search",
        params={"query": "SECRET-QUERY"},
    ),
    _sleep(PAUSE),
    _ev(
        "agent_tool_result",
        role="panellist:1",
        tool_name="web_search",
        result="SECRET-RESULT",
        status="success",
    ),
    _sleep(PAUSE),
    _ev("contribution", role="panellist:1", name="Ada", round=1, text="My view."),
    _sleep(0.2),
    _ev("complete"),
]

DEBATE_STATE = {"config": {"state": "running", "current_round": 1, "agents": {}}, "turns": []}
PANEL_STATE = {
    "config": {
        "state": "running",
        "current_round": 1,
        "agents": {
            "moderator": {"display_name": "Moderator"},
            "panellist:1": {"display_name": "Ada"},
        },
    },
    "turns": [],
}

PROBE = r"""
<script>
(function () {
  const KIND = '__KIND__';
  const SCENARIOS = __SCENARIOS__;
  const out = { errors: [], kind: KIND, runs: {} };
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
  let trace = null;
  let t0 = 0;
  function snapshot() {
    const root = document.querySelector('.working-status');
    if (!root) return { gone: true };
    const dot = root.querySelector('.working-dots span');
    return {
      visible: !root.hidden,
      label: (root.querySelector('.working-label') || {}).textContent || '',
      step: root.querySelector('.working-step').textContent,
      time: root.querySelector('.working-time').textContent,
      role: root.getAttribute('role'),
      live: root.getAttribute('aria-live'),
      timeHidden: root.querySelector('.working-time').getAttribute('aria-hidden'),
      animation: dot ? getComputedStyle(dot).animationName : '',
      reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
      thinkingBox: !!document.querySelector('.thought-process.thinking'),
      waiting: document.querySelectorAll('.awaiting-reply').length,
      page: document.body.innerText.includes('SECRET'),
    };
  }
  function record() {
    if (!trace) return;
    const s = snapshot();
    const key = JSON.stringify(s);
    if (trace.length && trace[trace.length - 1].key === key) return;
    trace.push({ key, at: Date.now() - t0, s });
  }
  new MutationObserver(record).observe(document.documentElement, {
    subtree: true, childList: true, attributes: true, characterData: true });

  async function chatScenario(name) {
    trace = []; t0 = Date.now();
    record();
    document.getElementById('chat-input').value = name;
    sendMessageWithSSE(conversationId, name);
    if (name === 'stop') {
      await wait(() => document.querySelector('.working-status'), 20000);
      await new Promise(r => setTimeout(r, 1200));
      document.getElementById('btn-send').click();
    }
    // The stream opens once the session is ready; the turn is over when it closes again
    await wait(() => isRequestInProgress(), 20000);
    await wait(() => !isRequestInProgress(), 60000);
    await new Promise(r => setTimeout(r, 300));
    record();
    out.runs[name] = {
      trace: trace.map(t => t.s),
      end: {
        gone: !document.querySelector('.working-status'),
        waiting: document.querySelectorAll('.awaiting-reply').length,
        text: document.getElementById('chat-messages').innerText,
        sendLabel: document.getElementById('btn-send').getAttribute('aria-label'),
      },
    };
  }
  async function streamPage() {
    trace = []; t0 = Date.now();
    record();
    await wait(() => trace.some(t => !t.s.gone), 60000);
    await wait(() => trace.length && trace[trace.length - 1].s.gone, 60000);
    await new Promise(r => setTimeout(r, 300));
    out.runs[KIND] = { trace: trace.map(t => t.s), end: { text: document.body.innerText } };
  }
  async function run() {
    try {
      if (KIND === 'chat') {
        await wait(() => typeof sendMessageWithSSE === 'function' && typeof loadChatHistory === 'function', 60000);
        await new Promise(r => setTimeout(r, 1500));
        for (const name of SCENARIOS) await chatScenario(name);
      } else {
        await streamPage();
      }
    } catch (err) {
      out.errors.push(String(err && err.stack || err));
    }
    await fetch('/__report', { method: 'POST', body: JSON.stringify(out) });
  }
  window.addEventListener('load', () => setTimeout(run, 0));
})();
</script>
"""


class Served:
    """The application behind a small server that scripts the event streams."""

    def __init__(self, client: TestClient, path: str, kind: str, scenarios: list[str]) -> None:
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        self.connections: dict[str, int] = {}
        self.closing = threading.Event()
        lock = threading.Lock()
        served = self
        self.path = path
        probe = PROBE.replace("__KIND__", kind).replace("__SCENARIOS__", json.dumps(scenarios))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: bytes, kind_: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind_)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self, steps: list[Step], retry_ms: int | None = None) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    if retry_ms is not None:
                        self.wfile.write(f"retry: {retry_ms}\n\n".encode())
                        self.wfile.flush()
                    for step in steps:
                        if step[0] == "sleep":
                            time.sleep(step[1])
                            continue
                        frame = f"event: {step[1]}\ndata: {json.dumps(step[2])}\n\n"
                        self.wfile.write(frame.encode())
                        self.wfile.flush()
                except OSError:
                    return None

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/__report":
                    served.report = json.loads(body)
                    served.reported.set()
                self._send(204, b"", "text/plain")

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/stream/chat":
                    name = parse_qs(parsed.query).get("message", ["plain"])[0]
                    count = served.connections[name] = served.connections.get(name, 0) + 1
                    if name == "dropped":
                        # The connection ends without a word: the page must not look stalled
                        self._stream([STATUS, _sleep(0.5)], retry_ms=60000)
                        return
                    self._stream(CHAT[name])
                    return
                if parsed.path == "/stream/conversation":
                    quiet_live_view(self, served.closing)
                    return
                if parsed.path == "/stream/debate":
                    self._stream(DEBATE)
                    return
                if parsed.path == "/stream/panel":
                    self._stream(PANEL)
                    return
                if parsed.path == "/debate/api/state":
                    self._send(200, json.dumps(DEBATE_STATE).encode(), "application/json")
                    return
                if parsed.path == "/panel/api/state":
                    self._send(200, json.dumps(PANEL_STATE).encode(), "application/json")
                    return
                with lock:
                    answer = client.get(self.path, follow_redirects=False)
                kind_ = answer.headers.get("content-type", "application/octet-stream")
                body = answer.content
                if parsed.path == served.path and kind_.startswith("text/html"):
                    text = body.decode("utf-8")
                    text = text.replace("</body>", probe + "</body>", 1)
                    body = text.encode("utf-8")
                self._send(answer.status_code, body, kind_)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()


def open_page(
    client: TestClient,
    path: str,
    kind: str,
    scenarios: list[str],
    *flags: str,
) -> dict[str, Any]:
    assert BROWSER is not None
    served = Served(client, path, kind, scenarios)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-working-", ignore_cleanup_errors=True
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
                    *flags,
                    f"http://127.0.0.1:{served.port}{path}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = served.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, f"the {kind} page did not report in {RUN_BUDGET_S} s"
        return served.report
    finally:
        served.close()


@pytest.fixture(scope="module")
def chat(chat_client: tuple[TestClient, int]) -> dict[str, Any]:  # noqa: F811
    client, cid = chat_client
    report = open_page(client, f"/chat/{cid}", "chat", list(CHAT))
    assert report["errors"] == []
    return report["runs"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def reduced(chat_client: tuple[TestClient, int]) -> dict[str, Any]:  # noqa: F811
    client, cid = chat_client
    report = open_page(client, f"/chat/{cid}", "chat", ["plain"], "--force-prefers-reduced-motion")
    assert report["errors"] == []
    return report["runs"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def debate(chat_client: tuple[TestClient, int]) -> dict[str, Any]:  # noqa: F811
    client, cid = chat_client
    return open_page(client, f"/debate/{cid}", "debate", [])["runs"]["debate"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def panel(chat_client: tuple[TestClient, int]) -> dict[str, Any]:  # noqa: F811
    client, cid = chat_client
    return open_page(client, f"/panel/{cid}", "panel", [])["runs"]["panel"]  # type: ignore[no-any-return]


def shown(trace: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The (indicator line, status line) pairs seen while the indicator showed, in order."""
    seen: list[tuple[str, str]] = []
    for s in trace:
        if s.get("gone") or not s["visible"]:
            continue
        pair = (s["label"], s["step"])
        if not seen or seen[-1] != pair:
            seen.append(pair)
    return seen


def steps(trace: list[dict[str, Any]]) -> list[str]:
    return [step for _, step in shown(trace) if step]


def test_sent_shows_thinking_at_once_in_the_replys_place(chat: dict[str, Any]) -> None:
    trace = chat["plain"]["trace"]
    first = next(s for s in trace if not s.get("gone"))
    assert first["visible"] is True
    assert first["label"] == "Thinking..."
    # The bubble waits, out of sight, while the indicator stands in for it
    assert first["waiting"] == 1


def test_the_reply_replaces_the_indicator(chat: dict[str, Any]) -> None:
    trace = chat["plain"]["trace"]
    assert any(s.get("visible") is False for s in trace), "the indicator never stepped aside"
    hidden = next(s for s in trace if s.get("visible") is False)
    assert hidden["waiting"] == 0
    end = chat["plain"]["end"]
    assert end["gone"] is True
    assert "Hello there" in end["text"]


def test_the_reasoning_box_keeps_its_behaviour_and_the_indicator_steps_aside(
    chat: dict[str, Any],
) -> None:
    trace = chat["reasoning"]["trace"]
    aside = [s for s in trace if s.get("visible") is False and s["thinkingBox"]]
    assert aside, "the indicator should step aside while the reasoning box shows"
    assert chat["reasoning"]["end"]["gone"] is True
    assert "Decided." in chat["reasoning"]["end"]["text"]


def test_each_kind_of_step_has_its_line_in_order(chat: dict[str, Any]) -> None:
    seen = steps(chat["tools"]["trace"])
    wanted = [
        "Searching the web",
        "Reading report.pdf",
        "Running a tool: srv__lookup_thing",
        "Creating a chart",
        "Working with an agent",
        "Compacting the conversation",
    ]
    positions = [seen.index(line) for line in wanted]
    assert positions == sorted(positions), seen


def test_a_step_ends_back_to_thinking(chat: dict[str, Any]) -> None:
    pairs = shown(chat["tools"]["trace"])
    assert ("Thinking...", "") in pairs[1:]
    assert pairs[-1] == ("Thinking...", "") or pairs[-1][1] != ""


def test_no_tool_input_or_result_reaches_the_indicator(chat: dict[str, Any]) -> None:
    trace = chat["tools"]["trace"]
    for s in trace:
        if s.get("gone"):
            continue
        assert "SECRET" not in s["label"] + s["step"] + s["time"]
        assert "private" not in s["step"], "a path is never shown, only the file's name"
    assert "SECRET-QUERY" not in " ".join(steps(trace))


def test_the_time_shows_after_a_few_seconds_and_not_before(chat: dict[str, Any]) -> None:
    trace = chat["slow"]["trace"]
    on_step = [s for s in trace if not s.get("gone") and s["step"] == "Reading a web page"]
    assert on_step[0]["time"] == ""
    timed = [s["time"] for s in on_step if s["time"]]
    assert timed, "no elapsed time was shown on a seven second step"
    assert timed[0].strip() in {"(5s)", "(6s)"}, timed
    assert chat["slow"]["end"]["gone"] is True


def test_the_time_is_hidden_from_a_screen_reader_and_the_block_is_polite(
    chat: dict[str, Any],
) -> None:
    visible = [s for s in chat["slow"]["trace"] if not s.get("gone")]
    assert {s["role"] for s in visible} == {"status"}
    assert {s["live"] for s in visible} == {"polite"}
    assert {s["timeHidden"] for s in visible} == {"true"}


def test_a_turn_with_no_text_clears_cleanly(chat: dict[str, Any]) -> None:
    end = chat["toolsonly"]["end"]
    assert end["gone"] is True
    assert end["waiting"] == 0


def test_stop_clears_the_indicator(chat: dict[str, Any]) -> None:
    run = chat["stop"]
    assert any(not s.get("gone") for s in run["trace"])
    assert run["end"]["gone"] is True
    assert run["end"]["waiting"] == 0
    assert run["end"]["sendLabel"] == "Send"


def test_an_error_clears_the_indicator_and_says_so(chat: dict[str, Any]) -> None:
    run = chat["error"]
    assert any(not s.get("gone") for s in run["trace"])
    assert run["end"]["gone"] is True
    assert "The model could not be reached." in run["end"]["text"]


def test_a_dropped_connection_clears_the_indicator_and_says_so(chat: dict[str, Any]) -> None:
    run = chat["dropped"]
    assert any(not s.get("gone") for s in run["trace"])
    assert run["end"]["gone"] is True
    assert run["end"]["waiting"] == 0
    assert "Connection lost." in run["end"]["text"]


def test_the_turn_ends_with_nothing_left_behind(chat: dict[str, Any]) -> None:
    for name, run in chat.items():
        assert run["end"]["gone"] is True, name
        assert run["end"]["waiting"] == 0, name
        assert run["trace"][-1].get("gone") is True, name


def test_the_dots_move_and_stand_still_under_reduced_motion(
    chat: dict[str, Any], reduced: dict[str, Any]
) -> None:
    moving = next(s for s in chat["plain"]["trace"] if not s.get("gone"))
    still = next(s for s in reduced["plain"]["trace"] if not s.get("gone"))
    # A machine with its own animations switched off already asks for reduced motion
    assert moving["animation"] == ("none" if moving["reduce"] else "working-pulse")
    assert still["reduce"] is True
    assert still["animation"] == "none"


def test_the_dots_animate_unless_motion_is_reduced() -> None:
    css = (WEB / "static" / "css" / "chat_status.css").read_text(encoding="utf-8")
    base, _, reduced_rules = css.partition("@media (prefers-reduced-motion: reduce)")
    assert "animation: working-pulse" in base
    assert "@keyframes working-pulse" in base
    assert "animation: none" in reduced_rules


def test_debate_shows_who_is_preparing_and_the_tool_in_use(debate: dict[str, Any]) -> None:
    seen = steps(debate["trace"])
    assert "The judge is considering" in seen
    assert "Pro is preparing round 1" in seen
    assert "Searching the web" in seen
    assert seen.index("Pro is preparing round 1") < seen.index("Searching the web")
    assert debate["trace"][-1].get("gone") is True
    assert all("SECRET" not in s.get("step", "") for s in debate["trace"])
    assert {s["live"] for s in debate["trace"] if not s.get("gone")} == {"polite"}


def test_panel_shows_who_is_preparing_and_the_tool_in_use(panel: dict[str, Any]) -> None:
    seen = steps(panel["trace"])
    assert "The moderator is considering" in seen
    assert "Ada is preparing round 1" in seen
    assert "Searching the web" in seen
    assert seen.index("Ada is preparing round 1") < seen.index("Searching the web")
    assert panel["trace"][-1].get("gone") is True
    assert all("SECRET" not in s.get("step", "") for s in panel["trace"])


def test_the_friendly_names_come_from_one_table_and_unknown_tools_show_their_name() -> None:
    node = pytest.importorskip("shutil").which("node")
    if node is None:
        pytest.skip("no Node.js")
    script = WEB / "static" / "js" / "working_status.js"
    code = (
        "global.window={};global.document={};"
        f"require({json.dumps(str(script))});"
        "const w=window.SparkWorking;"
        "console.log(JSON.stringify(["
        "w.toolLabel('web_search',{query:'x'}),"
        "w.toolLabel('read_pdf',{path:'/a/b/report.pdf'}),"
        "w.toolLabel('read_pdf',{}),"
        "w.toolLabel('srv__thing',{a:1}),"
        "w.toolLabel('',{}),"
        "w.elapsedText(5000),w.elapsedText(65000)]))"
    )
    done = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        "Searching the web",
        "Reading report.pdf",
        "Reading a PDF",
        "Running a tool: srv__thing",
        "Running a tool",
        "5s",
        "1m 05s",
    ]
