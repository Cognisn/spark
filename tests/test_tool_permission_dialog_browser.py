"""The tool permission dialogue is compact, clear and keyboard operable.

A headless Chromium opens the real ``chat.html`` as the application renders it. Each scenario
raises a permission prompt in the page, acts on it the way a person would (a click, or the
Escape key) and reports what the page did: the decision it sent (the answer's network call is
stubbed and recorded in the page), whether the dialogue closed, where focus went and how big
the dialogue is. The real clock decides when Bootstrap's fade ends.

Without a Chromium the tests are skipped, as the other browser tests are.
"""

from __future__ import annotations

import subprocess
import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

import tests.test_prompt_dialog_browser as prompt_dialog
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
  const out = { errors: [], scenario: location.hash.slice(1), decisions: [] };
  window.addEventListener('error', e => {
    if (!String(e.message).startsWith('ResizeObserver loop')) out.errors.push(String(e.message));
  });
  const realFetch = window.fetch.bind(window);
  window.fetch = (url, opts) => {
    if (/\/chat\/permission\/respond/.test(String(url))) {
      out.decisions.push(JSON.parse(opts.body).decision);
      return Promise.resolve(new Response('{"status":"ok"}', { status: 200 }));
    }
    return realFetch(url, opts);
  };
  const el = () => document.getElementById('permissionModal');
  const isOpen = () => el().classList.contains('show');
  const tick = ms => new Promise(r => setTimeout(r, ms));
  const params = { url: 'https://example.com/reports/2026/quarterly-summary.html', max_length: 5000,
                   note: 'x'.repeat(400) };
  const choices = { deny: 0, once: 1, chat: 2, always: 3 };
  async function run() {
    try {
      showPermissionPrompt({ request_id: 'p1', tool_name: 'web_fetch', params, stream_id: 's1' });
      await tick(1200);
      out.openAfterAsk = isOpen();
      out.focusOnOpen = document.activeElement && document.activeElement.id;
      out.title = document.getElementById('permission-tool').textContent;
      out.role = el().getAttribute('role');
      out.labelledby = document.getElementById(el().getAttribute('aria-labelledby')).textContent;
      out.describedby = !!document.getElementById(el().getAttribute('aria-describedby'));
      out.keyboardOff = el().getAttribute('data-bs-keyboard');
      const buttons = [...el().querySelectorAll('.tp-actions button')];
      out.buttons = buttons.map(b => ({
        name: b.getAttribute('aria-label'), title: b.title, onclick: b.getAttribute('onclick'),
        description: document.getElementById(b.getAttribute('aria-describedby')).textContent,
        top: Math.round(b.getBoundingClientRect().top),
      }));
      out.focusable = [...el().querySelectorAll('button, [href], input, select, textarea, [tabindex]')]
        .filter(n => n.tabIndex >= 0).map(n => n.getAttribute('aria-label'));
      const box = el().querySelector('.modal-content').getBoundingClientRect();
      out.width = Math.round(box.width);
      out.height = Math.round(box.height);
      out.left = Math.round(box.left);
      out.right = Math.round(box.right);
      out.viewport = window.innerWidth;
      out.scrollWidth = document.documentElement.scrollWidth;
      const s = out.scenario.split(',')[0];
      if (s in choices) {
        const btn = buttons[choices[s]];
        btn.focus();
        btn.click();
      } else if (s === 'escape') {
        document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      }
      await tick(1500);
      out.openAfterAnswer = isOpen();
      out.backdropsAfter = document.querySelectorAll('.modal-backdrop').length;
      out.focusAfter = document.activeElement && document.activeElement.id;
    } catch (err) {
      out.errors.push(String(err && err.stack || err));
    }
    await realFetch('/__report', { method: 'POST', body: JSON.stringify(out) });
  }
  window.addEventListener('load', () => setTimeout(run, 500));
})();
</script>
"""

DESKTOP = (1280, 800)
PHONE = (390, 760)
NAMES = [
    "Deny",
    "Approve once",
    "Always allow in this conversation",
    "Always allow in all conversations",
]


def _run(client: TestClient, cid: int, scenario: str, size: tuple[int, int]) -> dict[str, Any]:
    assert BROWSER is not None
    served = prompt_dialog.Served(client, cid)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-permission-", ignore_cleanup_errors=True
        ) as profile:
            browser = subprocess.Popen(
                [
                    BROWSER,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--window-size={size[0]},{size[1]}",
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


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt_dialog, "PROBE", PROBE)


def test_the_dialogue_appears_for_a_permission_request_and_is_announced(
    chat_client: tuple[TestClient, int],  # noqa: F811
    probe: None,
) -> None:
    client, cid = chat_client
    report = _run(client, cid, "open", DESKTOP)
    assert report["openAfterAsk"] is True
    assert report["title"] == "web_fetch wants to run"
    assert report["role"] == "alertdialog"
    assert report["labelledby"] == "web_fetch wants to run"
    assert report["describedby"] is True
    # Bootstrap's own Escape is off, so Escape can answer the request
    assert report["keyboardOff"] == "false"
    assert report["decisions"] == []


def test_each_choice_has_a_full_name_a_tooltip_a_description_and_its_response(
    chat_client: tuple[TestClient, int],  # noqa: F811
    probe: None,
) -> None:
    client, cid = chat_client
    report = _run(client, cid, "open", DESKTOP)
    assert [b["name"] for b in report["buttons"]] == NAMES
    assert [b["onclick"] for b in report["buttons"]] == [
        "respondPermission('denied')",
        "respondPermission('once')",
        "respondPermission('allowed')",
        "respondPermission('allowed_global')",
    ]
    for button in report["buttons"]:
        assert button["title"]
        assert button["description"]


@pytest.mark.parametrize(
    ("choice", "decision"),
    [("deny", "denied"), ("once", "once"), ("chat", "allowed"), ("always", "allowed_global")],
)
def test_each_choice_sends_its_response_closes_the_dialogue_and_returns_focus(
    chat_client: tuple[TestClient, int],  # noqa: F811
    probe: None,
    choice: str,
    decision: str,
) -> None:
    client, cid = chat_client
    report = _run(client, cid, choice, DESKTOP)
    assert report["decisions"] == [decision]
    assert report["openAfterAnswer"] is False
    assert report["backdropsAfter"] == 0
    assert report["focusAfter"] == "chat-input"


def test_the_keyboard_starts_on_deny_in_a_fixed_tab_order_and_escape_denies(
    chat_client: tuple[TestClient, int],  # noqa: F811
    probe: None,
) -> None:
    client, cid = chat_client
    opened = _run(client, cid, "open", DESKTOP)
    assert opened["focusOnOpen"] == "permission-deny"
    assert opened["focusable"] == NAMES
    escaped = _run(client, cid, "escape", DESKTOP)
    assert escaped["decisions"] == ["denied"]
    assert escaped["openAfterAnswer"] is False
    assert escaped["backdropsAfter"] == 0
    assert escaped["focusAfter"] == "chat-input"


@pytest.mark.parametrize("size", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_the_dialogue_stays_compact_with_one_row_of_buttons_and_no_horizontal_scroll(
    chat_client: tuple[TestClient, int],  # noqa: F811
    probe: None,
    size: tuple[int, int],
) -> None:
    client, cid = chat_client
    report = _run(client, cid, "open", size)
    assert report["width"] <= 340
    assert report["width"] >= min(300, report["viewport"] - 16)
    assert report["height"] <= 140
    assert report["left"] >= 0
    assert report["right"] <= report["viewport"]
    assert report["scrollWidth"] <= report["viewport"]
    assert len({b["top"] for b in report["buttons"]}) == 1
    if size == PHONE:
        assert report["viewport"] <= 400
