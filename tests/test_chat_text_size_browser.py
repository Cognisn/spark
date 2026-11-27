"""The conversation's type scale in a real browser (question 99: option B).

A headless Chromium opens the real chat page, as the application serves it, for a reply
that holds headings, a paragraph, a list, a code block and a table, and reads the computed
size of each element. Sizes are compared as multiples of the root font size, so they are
checked in ``rem`` as written: a fixed pixel size, or one that ignores the reader's browser
size, fails the check.

* The agreed scale holds in light and dark, at desktop and phone width: replies and user
  messages 0.875rem at a line height of 1.45, headings 1.375, 1.2, 1.05 and 0.9375rem,
  table cells 0.8125rem and a paragraph gap of 0.625rem.
* With the root size doubled (what 200% text zoom does, on a window of 700 pixels, a 1400 pixel window zoomed to 200%), every size is unchanged as a
  multiple of the root, so nothing is fixed in pixels, and the page still fits the window.
* Mutation check: with the scale's variables changed (the old oversized headings, a larger
  body), the same check reports each element that moved, so it would catch a regression.

Without a Chromium the tests are skipped, as the other browser tests are.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.database import conversations as convdb
from spark.database import messages as msgdb
from tests import test_chat_page_browser as page
from tests.test_chat_page_browser import USER, chat_client  # noqa: F401  (fixture)
from tests.test_preview_renderers import BROWSER

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(2 * page.RUN_BUDGET_S + 60),
]

REPLY = """# Release planning summary

A long paragraph that explains how the figures were put together, with `inline code`.

## Key findings

- Sales rose in every region

### Steps

1. Open the report

#### Notes

```python
print("total")
```

| Quarter | Sales |
|---|---|
| Q1 | 10 |
"""

# What the agreed scale says, in rem (or a pure ratio for the line height).
AGREED = {
    "user": 0.875,
    "reply": 0.875,
    "h1": 1.375,
    "h2": 1.2,
    "h3": 1.05,
    "h4": 0.9375,
    "cell": 0.8125,
    "gap": 0.625,
    "leading": 1.45,
}

PROBE = r"""
<style>__CSS__</style>
<script>
(function () {
  const out = { errors: [] };
  window.addEventListener('error', e => {
    if (!String(e.message).startsWith('ResizeObserver loop')) out.errors.push(String(e.message));
  });
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
  async function run() {
    try {
      out.history = !!(await wait(() => document.querySelector(
        '.chat-message.assistant .markdown-content table td'), 90000));
      const root = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const px = (sel, prop) => {
        const el = document.querySelector(sel);
        return el ? parseFloat(getComputedStyle(el)[prop]) : null;
      };
      const reply = '.chat-message.assistant .markdown-content ';
      const replyBubble = px('.chat-message.assistant', 'fontSize');
      out.root = root;
      out.sizes = {
        user: px('.chat-message.user', 'fontSize') / root,
        reply: replyBubble / root,
        h1: px(reply + 'h1', 'fontSize') / root,
        h2: px(reply + 'h2', 'fontSize') / root,
        h3: px(reply + 'h3', 'fontSize') / root,
        h4: px(reply + 'h4', 'fontSize') / root,
        cell: px(reply + 'td', 'fontSize') / root,
        gap: px(reply + 'p', 'marginBottom') / root,
        leading: px('.chat-message.assistant', 'lineHeight') / replyBubble,
      };
      out.page = { scroll: document.documentElement.scrollWidth,
                   client: document.documentElement.clientWidth };
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


@pytest.fixture(scope="module")
def text_client(chat_client: tuple[TestClient, int]) -> tuple[TestClient, int]:  # noqa: F811
    client, _ = chat_client
    db = client.app.state.sync_db  # type: ignore[attr-defined]
    cid: int = db.call(convdb.create_conversation, "Type scale", "stub/model", USER)
    db.call(msgdb.add_message, cid, "user", "Summarise the quarterly sales.", 5, USER)
    blocks = [{"type": "text", "text": REPLY}]
    db.call(msgdb.add_message, cid, "assistant", json.dumps(blocks), 5, USER)
    return client, cid


def read(
    client_cid: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    theme: str,
    mode: str,
    size: str,
    css: str = "",
) -> dict[str, Any]:
    monkeypatch.setattr(page, "PROBE", PROBE.replace("__CSS__", css))
    report = page.open_page(*client_cid, theme, mode, size)
    assert report["errors"] == []
    assert report["history"] is True
    return report


def mismatches(sizes: dict[str, float]) -> list[str]:
    """Each element whose computed size is not the agreed one."""
    return [
        f"{name}: {sizes[name]:.4f} != {agreed:.4f}"
        for name, agreed in AGREED.items()
        if abs(sizes[name] - agreed) > 0.005
    ]


@pytest.mark.parametrize(
    ("theme", "mode", "size"),
    [("dark", "desktop", "1400,1000"), ("light", "phone", "390,844")],
)
def test_the_agreed_type_scale_holds(
    text_client: tuple[TestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    theme: str,
    mode: str,
    size: str,
) -> None:
    report = read(text_client, monkeypatch, theme, mode, size)
    assert report["theme"] == theme
    assert mismatches(report["sizes"]) == []


def test_every_size_follows_the_root_size_at_200_percent(
    text_client: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = read(text_client, monkeypatch, "light", "desktop", "700,1000", "html{font-size:200%}")
    assert report["root"] >= 32, "the root size did not double"
    assert mismatches(report["sizes"]) == []
    assert report["page"]["scroll"] <= report["page"]["client"]


def test_the_check_catches_a_changed_scale(
    text_client: tuple[TestClient, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: the old oversized headings and a larger body must be reported."""
    css = (
        ":root{--chat-h1-size:2.5rem;--chat-h2-size:2rem;--chat-text-size:1rem;"
        "--chat-table-size:0.875rem;--chat-paragraph-gap:0.75rem}"
    )
    report = read(text_client, monkeypatch, "dark", "desktop", "1400,1000", css)
    named = {line.split(":")[0] for line in mismatches(report["sizes"])}
    assert {"user", "reply", "h1", "h2", "cell", "gap"} <= named
