"""The chat page's "Thought process" line (``static/js/chat.js`` and ``sse-client.js``).

Under Node, with a minimal DOM, the real scripts are loaded and driven by the events the
stream sends:

* a streamed reply with reasoning shows "Thinking..." above the reply, open, with the
  text growing, and collapses to "Thought process" when the answer starts;
* the reasoning is outside the reply's bubble and its copy text;
* a reply without reasoning shows nothing extra;
* reasoning before a tool call, with no reply text, keeps its own line;
* a reloaded conversation shows each reply's reasoning collapsed;
* the line is a button with ``aria-expanded`` and ``aria-controls``, operable by click
  (a native button, so the keyboard too), and its styles use the theme's variables.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
CHAT_JS = WEB / "static" / "js" / "chat.js"
SSE_JS = WEB / "static" / "js" / "sse-client.js"
SPARK_CSS = WEB / "static" / "css" / "spark.css"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = []; this.attrs = {}; this.dataset = {}; this.style = {};
    this.className = ''; this.hidden = false; this.parent = null;
    this.listeners = {}; this._text = ''; this.type = ''; this.scrollTop = 0; this.scrollHeight = 0;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  get id() { return this.attrs.id; }
  set id(v) { this.attrs.id = v; }
  get classList() {
    const self = this;
    return {
      add: (c) => { self.className = (self.className + ' ' + c).trim(); },
      remove: (c) => { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(' '); },
      contains: (c) => self.className.split(/\s+/).includes(c),
    };
  }
  get firstElementChild() { return this.children[0] || null; }
  appendChild(c) { if (c.parent) c.remove(); c.parent = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    if (c.parent) c.remove();
    c.parent = this;
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  remove() {
    if (this.parent) { this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; }
  }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() { (this.listeners.click || []).forEach((f) => f()); }
  insertAdjacentHTML() {}
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set innerHTML(html) {
    this.children = []; this._text = '';
    if (!/</.test(html)) { this._text = String(html); return; }
    const tag = /<(\/?)(\w+)([^>]*)>/g; let m; const stack = [this];
    const VOID = new Set(['br', 'img', 'input', 'hr']);
    let last = 0;
    while ((m = tag.exec(html))) {
      const gap = html.slice(last, m.index).trim();
      if (gap) stack[stack.length - 1]._text += gap;
      last = tag.lastIndex;
      if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
      const el = new El(m[2]); const attr = /([\w-]+)="([^"]*)"/g; let a;
      while ((a = attr.exec(m[3]))) {
        if (a[1] === 'class') el.className = a[2]; else el.attrs[a[1]] = a[2];
      }
      stack[stack.length - 1].appendChild(el);
      if (!VOID.has(m[2].toLowerCase()) && !m[3].endsWith('/')) stack.push(el);
    }
  }
  matches(sel) {
    return sel.split(/(?=[.\[])/).every((part) => {
      if (part.startsWith('.')) return this.className.split(/\s+/).includes(part.slice(1));
      if (part.startsWith('[')) return (part.slice(1, -1)) in this.attrs;
      return this.tagName === part.toUpperCase();
    });
  }
  querySelector(sel) {
    for (const c of this.children) {
      if (c.matches(sel)) return c;
      const d = c.querySelector(sel); if (d) return d;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    for (const c of this.children) { if (c.matches(sel)) out.push(c); out.push(...c.querySelectorAll(sel)); }
    return out;
  }
}

const byId = {};
globalThis.window = globalThis;
globalThis.document = {
  createElement: (t) => new El(t),
  getElementById: (id) => (byId[id] = byId[id] || new El('div')),
  querySelectorAll: (sel) => byId['chat-messages'].querySelectorAll(sel.replace('#chat-messages ', '')),
};
const sources = [];
globalThis.EventSource = class {
  constructor(url) { this.url = url; this.handlers = {}; this.readyState = 1; sources.push(this); }
  addEventListener(t, f) { this.handlers[t] = f; }
  close() { this.readyState = 2; }
  emit(t, data) { this.handlers[t]({ data: JSON.stringify(data) }); }
};
globalThis.EventSource.CLOSED = 2;
globalThis.renderMarkdown = (t) => String(t);
globalThis.highlightCodeBlocks = () => {};
globalThis.renderMermaidDiagrams = () => {};
globalThis.escapeHtml = (t) => String(t);
globalThis.requestAnimationFrame = (f) => f();
globalThis.fetch = () => Promise.resolve({ json: () => Promise.resolve([]) });

for (const file of process.argv.slice(2, 4)) vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
// The tool panel and its group are other features: replaced after the scripts load
for (const name of ['addToolCallsToPanel', 'appendToolInlineIndicator', 'appendSystemMessage',
                    'finaliseStreamingToolGroup', 'appendStreamingToolStart']) {
  globalThis[name] = () => {};
}

const messages = byId['chat-messages'] = new El('div');
const wrappers = () => messages.children.filter((c) => c.className.includes('chat-message-wrapper'));
const tick = () => new Promise((r) => setTimeout(r, 0));
const thought = (w) => w.querySelector('.thought-process');
function snap(w) {
  const box = thought(w);
  const bubble = w.querySelector('.chat-message');
  const copy = bubble ? bubble.querySelector('[data-copy-text]') : null;
  const out = { thought: null, bubble: bubble ? bubble.querySelector('.markdown-content').textContent : null,
                copy: copy ? decodeURIComponent(copy.attrs['data-copy-text']) : null,
                order: w.children.map((c) => c.className.split(' ')[0]) };
  if (box) {
    const toggle = box.querySelector('.thought-toggle'); const body = box.querySelector('.thought-body');
    out.thought = {
      label: box.querySelector('.thought-label').textContent,
      expanded: toggle.getAttribute('aria-expanded'),
      controls: toggle.getAttribute('aria-controls'),
      bodyId: body.id, hidden: body.hidden, text: body.textContent,
      isButton: toggle.tagName === 'BUTTON', buttonType: toggle.type,
      thinking: box.className.includes('thinking'),
      outsideBubble: !(bubble && bubble.querySelector('.thought-process')),
    };
  }
  return out;
}
function reset() { messages.children = []; sources.length = 0; }

(async () => {
  const out = {};

  // A streamed reply: reasoning arrives, then the answer
  reset(); sendMessageWithSSE(7, 'hi'); await tick();
  const es = sources[0];
  es.emit('reasoning', { text: 'The user wants a name.' });
  out.thinking = snap(wrappers()[0]);
  es.emit('reasoning', { text: 'I will say Spark.' });
  out.growing = snap(wrappers()[0]);
  es.emit('response', { content: 'My name is Spark.', final: false });
  out.answering = snap(wrappers()[0]);
  es.emit('response', { content: 'My name is Spark.', final: true, usage: {} });
  out.finished = snap(wrappers()[0]);
  out.wrapperCount = wrappers().length;
  es.emit('complete', { status: 'ok' });

  // No reasoning
  reset(); sendMessageWithSSE(7, 'hi'); await tick();
  sources[0].emit('response', { content: 'Plain answer.', final: false });
  sources[0].emit('response', { content: 'Plain answer.', final: true, usage: {} });
  out.plain = snap(wrappers()[0]);
  sources[0].emit('complete', { status: 'ok' });

  // Reasoning before a tool call, with no reply text, then reasoning and the answer
  reset(); sendMessageWithSSE(7, 'hi'); await tick();
  const et = sources[0];
  et.emit('reasoning', { text: 'I should look it up.' });
  et.emit('tool_start', { tool_name: 'lookup', params: {}, tool_use_id: 't1' });
  out.afterTool = wrappers().map(snap);
  et.emit('reasoning', { text: 'Now I can answer.' });
  et.emit('response', { content: 'Found it.', final: true, usage: {} });
  out.betweenTools = wrappers().map(snap);
  et.emit('complete', { status: 'ok' });

  // The user opens the line while it is thinking: it stays open when the answer starts
  reset(); sendMessageWithSSE(7, 'hi'); await tick();
  const eu = sources[0];
  eu.emit('reasoning', { text: 'thinking hard' });
  thought(wrappers()[0]).querySelector('.thought-toggle').click();   // closes it
  thought(wrappers()[0]).querySelector('.thought-toggle').click();   // opens it
  eu.emit('response', { content: 'Answer.', final: false });
  out.userKept = snap(wrappers()[0]);
  eu.emit('response', { content: 'Answer.', final: true, usage: {} });
  eu.emit('complete', { status: 'ok' });

  // Reload: a reply with reasoning, one without, and a tool-use message with reasoning only
  reset();
  appendMessage({ role: 'assistant', content: 'Stored answer.', timestamp: null, reasoning: 'Stored thought.' });
  appendMessage({ role: 'assistant', content: 'No thought.', timestamp: null });
  appendMessage({ role: 'assistant', timestamp: null, reasoning: 'Before the tool.',
                  content: JSON.stringify([{ type: 'tool_use', id: 't', name: 'lookup', input: {} }]) });
  out.reloaded = wrappers().map(snap);
  const reloadedToggle = thought(wrappers()[0]).querySelector('.thought-toggle');
  reloadedToggle.click();
  out.clickedOpen = snap(wrappers()[0]).thought;
  reloadedToggle.click();
  out.clickedShut = snap(wrappers()[0]).thought;

  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def seen(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    harness = tmp_path_factory.mktemp("reasoning") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8", newline="\n")
    done = subprocess.run(
        ["node", str(harness), str(CHAT_JS), str(SSE_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    loaded: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return loaded


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
class TestUnderNode:
    def test_reasoning_shows_open_as_thinking_above_the_reply(self, seen: dict[str, Any]) -> None:
        t = seen["thinking"]["thought"]
        assert (t["label"], t["expanded"], t["hidden"], t["thinking"]) == (
            "Thinking...",
            "true",
            False,
            True,
        )
        assert t["text"] == "The user wants a name."
        assert seen["thinking"]["order"][0] == "thought-process"

    def test_the_reasoning_text_grows_as_more_arrives(self, seen: dict[str, Any]) -> None:
        text = seen["growing"]["thought"]["text"]
        assert text == "The user wants a name.\n\nI will say Spark."

    def test_the_line_collapses_when_the_answer_starts(self, seen: dict[str, Any]) -> None:
        t = seen["answering"]["thought"]
        assert (t["label"], t["expanded"], t["hidden"], t["thinking"]) == (
            "Thought process",
            "false",
            True,
            False,
        )
        assert seen["answering"]["bubble"] == "My name is Spark."

    def test_the_reasoning_is_not_in_the_bubble_or_its_copy_text(
        self, seen: dict[str, Any]
    ) -> None:
        done = seen["finished"]
        assert done["thought"]["outsideBubble"] is True
        assert done["bubble"] == "My name is Spark."
        assert done["copy"] == "My name is Spark."
        assert seen["wrapperCount"] == 1

    def test_a_reply_without_reasoning_shows_nothing_extra(self, seen: dict[str, Any]) -> None:
        assert seen["plain"]["thought"] is None
        assert "thought-process" not in seen["plain"]["order"]
        assert seen["plain"]["bubble"] == "Plain answer."

    def test_reasoning_before_a_tool_call_keeps_its_own_line(self, seen: dict[str, Any]) -> None:
        [only] = seen["afterTool"]
        assert only["bubble"] is None
        assert only["thought"]["text"] == "I should look it up."
        assert only["thought"]["expanded"] == "false"

    def test_reasoning_between_tool_calls_goes_above_the_next_reply(
        self, seen: dict[str, Any]
    ) -> None:
        first, second = seen["betweenTools"]
        assert first["thought"]["text"] == "I should look it up."
        assert second["thought"]["text"] == "Now I can answer."
        assert second["bubble"] == "Found it."
        assert second["thought"]["expanded"] == "false"

    def test_a_line_the_user_opened_stays_open_when_the_answer_starts(
        self, seen: dict[str, Any]
    ) -> None:
        assert seen["userKept"]["thought"]["expanded"] == "true"
        assert seen["userKept"]["thought"]["hidden"] is False

    def test_a_reloaded_reply_shows_its_reasoning_collapsed(self, seen: dict[str, Any]) -> None:
        stored, bare, tool_only = seen["reloaded"]
        t = stored["thought"]
        assert (t["label"], t["expanded"], t["hidden"], t["text"]) == (
            "Thought process",
            "false",
            True,
            "Stored thought.",
        )
        assert stored["bubble"] == "Stored answer." and stored["copy"] == "Stored answer."
        assert bare["thought"] is None
        assert tool_only["thought"]["text"] == "Before the tool."
        assert tool_only["thought"]["expanded"] == "false"

    def test_the_line_is_a_button_with_aria_expanded_that_a_click_toggles(
        self, seen: dict[str, Any]
    ) -> None:
        t = seen["reloaded"][0]["thought"]
        assert t["isButton"] is True and t["buttonType"] == "button"
        assert t["controls"] == t["bodyId"] and t["bodyId"]
        assert seen["clickedOpen"]["expanded"] == "true" and seen["clickedOpen"]["hidden"] is False
        assert seen["clickedShut"]["expanded"] == "false" and seen["clickedShut"]["hidden"] is True


class TestStyles:
    def test_the_line_is_muted_smaller_and_uses_the_theme_in_light_and_dark(self) -> None:
        css = SPARK_CSS.read_text(encoding="utf-8")
        start = css.index(".thought-process {")
        block = css[start:]
        assert "var(--app-text-muted)" in block and "var(--app-border)" in block
        assert "font-size: 0.8125rem" in block
        # Colours come from the theme's variables only, so both themes read well
        assert "#" not in block.replace("#chat", "")
        assert ".thought-toggle:focus-visible" in block
        assert ".thought-body[hidden]" in block
