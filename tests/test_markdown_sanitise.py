"""Markdown in SPARK's own pages goes through one sanitising renderer.

``static/js/markdown.js`` (``window.SparkMarkdown``) is the only place that parses
Markdown for SPARK's pages, and it passes every result through DOMPurify with one
configuration; ``renderMarkdown`` in ``main.js`` is a thin wrapper over it. The preview
frame keeps its own renderer inside its sandbox.

The static tests are a tripwire: no script or template other than the renderer (and the
preview frame's) calls marked or DOMPurify, ``renderMarkdown`` delegates, and nothing the
renderer writes carries an inline handler.

The browser tests open, in headless Chromium on the real clock, one harness page per
surface that renders the same hostile reply through that surface's real code: the chat
history, the chat stream (sanitised on every partial render), the live viewer stream
(``share.js``), a debate and a panel (their history), the skills viewer, help, an
action's result, the actions assistant bubble and the release notes. On each surface the
script, handler, ``javascript:`` URL, SVG, MathML, style, frame, form and clobbering
payloads are gone and none of them ran, while headings, tables, task lists, links (to a
new tab, without opener or referrer), the copy button (a delegated listener) and mermaid
blocks still work. The browser is the one :func:`tests.test_preview_renderers.chromium`
finds; without one the browser tests are skipped.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment

from tests.test_preview_renderers import chromium, stop_browser

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
STATIC = WEB / "static"
JS = STATIC / "js"
TEMPLATES = WEB / "templates"

# Only these files may call marked or DOMPurify: the app's renderer and the preview
# frame's own (inside its sandbox).
RENDERERS = {JS / "markdown.js", JS / "preview_frame.js"}

MARKED_CALL = re.compile(
    r"\bmarked\s*(?:\.\s*(?:parse|parseInline|lexer|parser|setOptions|use|Marked)\b|\s*\()"
)
PURIFY_CALL = re.compile(r"\bDOMPurify\b")
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=\s*[\"'`$]", re.I)


def _sources() -> list[Path]:
    return sorted(JS.glob("*.js")) + sorted(TEMPLATES.rglob("*.html"))


def _function(source: str, name: str) -> str:
    match = re.search(rf"function {name}\([^)]*\)\s*\{{.*?\n\}}", source, re.S)
    assert match is not None, name
    return match.group(0)


class TestTripwire:
    def test_no_script_or_template_parses_markdown_outside_the_renderer(self) -> None:
        offenders = []
        for path in _sources():
            if path in RENDERERS:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if MARKED_CALL.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert offenders == []

    def test_no_script_or_template_sanitises_with_its_own_configuration(self) -> None:
        offenders = [
            path.name
            for path in _sources()
            if path not in RENDERERS and PURIFY_CALL.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []

    def test_the_renderer_sanitises_everything_it_parses(self) -> None:
        source = (JS / "markdown.js").read_text(encoding="utf-8")
        render = _function(source, "render")
        assert "sanitize(markdown().parse(" in render
        assert "window.SparkMarkdown = {" in source
        assert re.search(r"safe:\s*render\b", source)

    def test_render_markdown_delegates_to_the_renderer(self) -> None:
        main = (JS / "main.js").read_text(encoding="utf-8")
        body = _function(main, "renderMarkdown")
        assert "SparkMarkdown.render(" in body
        assert "marked" not in body and "innerHTML" not in body
        stream = _function(main, "renderMarkdownInto")
        assert "SparkMarkdown.renderInto(" in stream
        assert "marked" not in stream and "innerHTML" not in stream

    def test_a_stream_sanitises_every_block_it_parses(self) -> None:
        body = _function((JS / "markdown.js").read_text(encoding="utf-8"), "renderInto")
        parsed = body.index("md.parser(block)")
        assert (
            "sanitizer().sanitize(html, { ...CONFIG, RETURN_DOM_FRAGMENT: true })" in body[parsed:]
        )
        assert "innerHTML" not in body

    def test_rendered_markdown_carries_no_inline_handlers(self) -> None:
        for name in ("markdown.js", "main.js"):
            source = (JS / name).read_text(encoding="utf-8")
            assert not INLINE_HANDLER.search(source), name
        assert "copyToClipboard" not in (JS / "main.js").read_text(encoding="utf-8")

    def test_mermaid_runs_at_its_strict_security_level(self) -> None:
        main = (JS / "main.js").read_text(encoding="utf-8")
        assert "securityLevel: 'strict'" in main
        assert "'loose'" not in main

    def test_every_page_loads_the_renderer_after_its_libraries(self) -> None:
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        marked = base.index('<script src="/static/vendor/marked/marked.umd.js"></script>')
        purify = base.index('<script src="/static/vendor/dompurify/purify.min.js"></script>')
        renderer = base.index('<script src="/static/js/markdown.js"></script>')
        assert marked < renderer and purify < renderer
        assert renderer < base.index("{% block scripts %}")
        assert "window.SparkMarkdown" not in base

    def test_each_surface_renders_through_the_renderer(self) -> None:
        for name in ("chat.js", "debate.js", "panel.js"):
            assert "renderMarkdown(" in (JS / name).read_text(encoding="utf-8"), name
        for name in ("chat.js", "share.js"):  # the streams
            assert "renderMarkdownInto(" in (JS / name).read_text(encoding="utf-8"), name
        assert "renderMarkdown(data.body)" in (TEMPLATES / "skills.html").read_text("utf-8")
        for name in ("help.html", "actions.html"):
            body = _function((TEMPLATES / name).read_text(encoding="utf-8"), "_renderMd")
            assert "SparkMarkdown.render(" in body, name
        bubble = _function((TEMPLATES / "chat.html").read_text("utf-8"), "_appendActionBubble")
        assert "SparkMarkdown.render(" in bubble


# -- In a browser --------------------------------------------------------------------

BROWSER = chromium()
RUN_BUDGET_S = 240
SURFACE_BUDGET_MS = 60_000

# The hostile reply: what a model could be steered into writing for everyone who reads
# the conversation. Every payload calls pwn(); a call is recorded if any of them runs.
REPLY = """## Report

Safe **bold** text and a [site](https://example.com/page) and an [anchor](#notes).

| Host | Count |
|---|---|
| a.example.com | 1 |

- [x] done
- [ ] open

```python
print("hello")
```

```mermaid
graph TD
  A["<img src=x onerror=pwn('mermaid')>"] --> B
```

```python" onmouseover="pwn('lang')
x = 1
```

<script>pwn('script')</script>
<img src="x" onerror="pwn('img-onerror')">
<a href="#" onclick="pwn('onclick')">click</a>
<details open ontoggle="pwn('ontoggle')"><summary>s</summary>x</details>

[js](javascript:pwn('js-link')) and [js2](JaVaScRiPt:pwn('js-case'))
<a href="&#106;avascript:pwn('js-entity')">js3</a>
<a href=" javascript:pwn('js-space')">js4</a>

<svg onload="pwn('svg-onload')"><script>pwn('svg-script')</script></svg>
<svg><animate onbegin="pwn('svg-animate')" attributeName="x" dur="1s"/></svg>
<math><mtext><img src=x onerror="pwn('math')"></mtext></math>

<style>body{display:none}</style>
<p style="position:fixed;inset:0;background:red">overlay</p>
<div class="position-fixed top-0 start-0 w-100 h-100 bg-white">bootstrap overlay</div>

<iframe src="javascript:pwn('iframe')"></iframe>
<object data="javascript:pwn('object')"></object>
<embed src="javascript:pwn('embed')">
<form action="javascript:pwn('form')"><input type="text" value="q" autofocus onfocus="pwn('input')"><button formaction="javascript:pwn('formaction')">go</button></form>
<p id="chat-messages" name="getElementById">clobber</p>
<button data-spark-copy="x" data-extra="1" onclick="pwn('button')">fake</button>

End of report.
"""

PRELUDE = """<script>
window.__pwned = [];
window.pwn = what => window.__pwned.push(what);
window.__copied = [];
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
  writeText: text => { window.__copied.push(text); return Promise.resolve(); } } });
window.__highlighted = 0;
window.hljs = { highlightElement(el) {
  window.__highlighted++; el.classList.add('hljs'); el.dataset.highlighted = 'yes'; } };
window.__mermaid = { config: null, sources: [] };
window.mermaid = {
  initialize(config) { window.__mermaid.config = config; },
  async render(id, source) { window.__mermaid.sources.push(source);
    return { svg: '<svg class="stub-diagram"></svg>' }; },
};
window.bootstrap = { Modal: class { constructor() {} show() {} hide() {}
  static getInstance() { return null; } static getOrCreateInstance() { return new this(); } } };
window.__routes = {};
window.fetch = async (url) => {
  const key = Object.keys(window.__routes).find(k => String(url).startsWith(k));
  const body = key ? window.__routes[key] : {};
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
};
// Debate and panel pages look up many controls; any the harness lacks is made on demand.
window.__autoStub = () => {
  const real = document.getElementById.bind(document);
  document.getElementById = id => {
    const found = real(id);
    if (found) return found;
    const made = document.createElement(/select$/.test(id) ? 'select' : 'div');
    made.id = id;
    (real('stubs') || document.body).appendChild(made);
    return made;
  };
};
</script>"""

CHECKS = """<script>
const REPLY = __REPLY__;
const BAD_TAGS = new Set(['script', 'style', 'iframe', 'frame', 'object', 'embed', 'form',
  'svg', 'math', 'link', 'meta', 'base', 'textarea', 'select', 'template']);
const URL_ATTRS = new Set(['href', 'src', 'action', 'formaction', 'xlink:href', 'data']);
function inspect(roots) {
  const bad = [];
  const out = { h2: 0, tables: 0, links: [], copy: 0, code: 0, hljs: 0, mermaid: 0,
    mermaidProcessed: 0, ticks: 0, ticksDisabled: 0, text: '' };
  for (const root of roots) {
    out.text += root.textContent;
    out.h2 += root.querySelectorAll('h2').length;
    out.tables += root.querySelectorAll('table').length;
    out.code += root.querySelectorAll('pre code').length;
    out.hljs += root.querySelectorAll('pre code.hljs').length;
    out.copy += root.querySelectorAll('.code-block-wrapper button[data-spark-copy]').length;
    out.mermaid += root.querySelectorAll('.mermaid').length;
    out.mermaidProcessed += root.querySelectorAll('.mermaid[data-processed]').length;
    out.ticks += root.querySelectorAll('input').length;
    out.ticksDisabled += root.querySelectorAll('input[type=checkbox][disabled]').length;
    root.querySelectorAll('a').forEach(a => out.links.push({
      href: a.getAttribute('href'), target: a.getAttribute('target'), rel: a.getAttribute('rel') }));
    root.querySelectorAll('*').forEach(el => {
      const tag = el.tagName.toLowerCase();
      const diagram = el.closest('svg.stub-diagram');
      if (BAD_TAGS.has(tag) && !diagram) bad.push('tag:' + tag);
      for (const a of el.attributes) {
        const name = a.name.toLowerCase();
        const value = a.value.replace(/[\\s\\u0000-\\u001f]/g, '').toLowerCase();
        if (name.startsWith('on')) bad.push('attr:' + name);
        if (name === 'style' || name === 'id' || name === 'name') bad.push('attr:' + name);
        if (name.startsWith('data-') && !['data-spark-copy', 'data-processed', 'data-highlighted'].includes(name))
          bad.push('attr:' + name);
        if (URL_ATTRS.has(name) && value.startsWith('javascript:')) bad.push('url:' + name);
      }
      for (const c of el.classList) {
        if (/^(position-|fixed-|top-|start-|w-|h-|bg-|vh-|vw-)/.test(c)) bad.push('class:' + c);
      }
    });
  }
  out.bad = bad;
  return out;
}
function pause(ms) { return new Promise(r => setTimeout(r, ms)); }
async function until(check, ms) {
  const start = Date.now();
  while (!check() && Date.now() - start < ms) await pause(25);
  return check();
}
function chunks(text, size) {
  const out = [];
  for (let i = size; i < text.length + size; i += size) out.push(text.slice(0, i));
  return out;
}
async function finish(surface, roots, extra) {
  await pause(600);  // let any handler that survived (onerror and the like) fire
  const result = inspect(roots);
  const button = roots.map(r => r.querySelector('.code-block-wrapper button[data-spark-copy]'))
    .find(Boolean);
  if (button) {
    button.querySelector('i').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await until(() => window.__copied.length, 2000);
    result.icon = button.querySelector('i').className;
  }
  result.copied = window.__copied;
  result.pwned = window.__pwned;
  result.mermaidConfig = window.__mermaid.config;
  result.mermaidSources = window.__mermaid.sources;
  Object.assign(result, extra || {});
  parent.postMessage({ surface, result }, '*');
}
window.addEventListener('error', e => parent.postMessage({ surface: __SURFACE__,
  error: String(e.message) }, '*'));
window.addEventListener('unhandledrejection', e => parent.postMessage({ surface: __SURFACE__,
  error: String(e.reason && e.reason.stack || e.reason) }, '*'));
</script>"""

PAGES: dict[str, dict[str, str]] = {
    "chat": {
        "body": '<div id="chat-messages"></div><div id="tool-panel"></div>',
        "scripts": "main.js preview.js chat.js share.js",
        "run": """
appendMessage({ role: 'assistant', content: REPLY });
appendMessage({ role: 'assistant', content: JSON.stringify([{ type: 'text', text: REPLY }]) });
const stepBad = [];
for (const partial of chunks(REPLY, 23)) {
  updateStreamingMessage(partial);
  const md = document.querySelector('#chat-messages .chat-message:last-child .markdown-content');
  stepBad.push(...inspect([md]).bad);
}
// Block by block, the stream draws what the whole reply would draw, at every step.
const differ = [];
const whole = document.createElement('div');
const piecewise = document.createElement('div');
for (const partial of chunks(REPLY, 17)) {
  SparkMarkdown.renderInto(piecewise, partial);
  whole.innerHTML = SparkMarkdown.render(partial);
  // (only the white space between blocks may differ)
  const same = el => el.innerHTML.replace(/>\\s+</g, '><').trim();
  if (same(whole) !== same(piecewise)) differ.push(partial.length);
}
finaliseStreamingMessage(REPLY);
const roots = [...document.querySelectorAll('#chat-messages .markdown-content')];
await until(() => document.querySelectorAll('.mermaid[data-processed]').length >= 3, 3000);
await finish('chat', roots, { stepBad: [...new Set(stepBad)], bubbles: roots.length, differ,
  highlighted: window.__highlighted });
""",
    },
    "live": {
        "body": '<div id="chat-messages"></div>',
        "scripts": "main.js chat.js share.js",
        "run": """
SparkSharing._onEvent('turn_started', { user_guid: 'someone-else', name: 'Alex', stream_id: 's1' });
const stepBad = [];
let sent = 0;
for (const partial of chunks(REPLY, 31)) {
  SparkSharing._onEvent('delta', { stream_id: 's1', text: partial.slice(sent) });
  sent = partial.length;
  stepBad.push(...inspect([document.querySelector('#live-turn .markdown-content')]).bad);
}
const roots = [document.querySelector('#live-turn .markdown-content')];
await finish('live', roots, { stepBad: [...new Set(stepBad)] });
""",
    },
    "debate": {
        "body": (
            '<div id="debate-root" data-conversation-id="1"></div><div id="judge-content"></div>'
            '<div id="pro-content"></div><div id="con-content"></div><div id="stubs" hidden></div>'
        ),
        "before": "window.__autoStub(); window.__routes = {'/debate/api/state': {config: {state: 'qa', current_round: 1}, exhibits: {}, turns: [{id: 1, status: 'complete', turn_type: 'argument', role: 'pro', round: 1, content: REPLY}, {id: 2, status: 'complete', turn_type: 'ruling', content: REPLY}]}, '/chat/': {agents: []}};",
        "scripts": "main.js debate.js",
        "run": """
await until(() => document.querySelector('#judge-content .judgement-card'), 5000);
const roots = [document.getElementById('pro-content'), document.getElementById('judge-content')];
await finish('debate', roots);
""",
    },
    "panel": {
        "body": (
            '<div id="panel-root" data-conversation-id="1"></div><div id="panel-thread"></div>'
            '<div id="stubs" hidden></div>'
        ),
        "before": "window.__autoStub(); window.__routes = {'/panel/api/state': {config: {state: 'qa', current_round: 1, agents: {}}, exhibits: {}, turns: [{id: 1, status: 'complete', turn_type: 'contribution', role: 'expert_1', round: 1, content: REPLY}, {id: 2, status: 'complete', turn_type: 'synthesis', content: REPLY}]}, '/chat/': {agents: []}};",
        "scripts": "main.js panel.js",
        "run": """
await until(() => document.querySelector('#panel-thread .judgement-card'), 5000);
// Each card's body (its last child) holds the Markdown; the card itself has its colour.
await finish('panel', [...document.getElementById('panel-thread').children].map(c => c.lastElementChild));
""",
    },
    "skills": {
        "body": (
            '<div id="invalid-skills"></div><div id="skills-list"></div>'
            '<div id="skill-view-title"></div><div id="skill-view-body"></div>'
            '<div id="skill-view-resources"></div><div id="skillViewModal"></div>'
        ),
        "before": "window.__routes = {'/skills/api/list': {skills: []}, '/skills/api/view': {name: 'report', body: REPLY, resources: []}};",
        "inline": "skills",
        "run": """
await viewSkill('report');
await finish('skills', [document.getElementById('skill-view-body')]);
""",
    },
    "help": {
        "body": '<div id="help-nav"></div><div id="help-content"></div>',
        "inline": "help",
        "run": """
await until(() => document.querySelector('#help-content .markdown-content'), 3000);
await finish('help', [document.getElementById('help-content')]);
""",
    },
    "actions": {
        "body": '<div id="result"></div>',
        "inline": "actions",
        "run": """
document.getElementById('result').innerHTML = `<div class="markdown-content">${_renderMd(REPLY)}</div>`;
await finish('actions', [document.getElementById('result')]);
""",
    },
    "action_bubble": {
        "body": '<div id="action-chat-messages"></div>',
        "before": "window._scrollActionChat = () => {};",
        "inline": "action_bubble",
        "run": """
_appendActionBubble('assistant', REPLY);
await finish('action_bubble', [document.querySelector('#action-chat-messages .markdown-content')]);
""",
    },
    "release_notes": {
        "body": '<div id="notes"></div>',
        "run": """
document.getElementById('notes').innerHTML = SparkMarkdown.safe(REPLY);
await finish('release_notes', [document.getElementById('notes')]);
""",
    },
}

TOP = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>markdown</title></head><body>
<script>
const SURFACES = __SURFACES__;
const BUDGET = __BUDGET__;
(async () => {
  const results = {};
  for (const surface of SURFACES) {
    const frame = document.createElement('iframe');
    const got = new Promise(resolve => {
      const timer = setTimeout(() => resolve({ timeout: BUDGET }), BUDGET);
      window.addEventListener('message', function on(e) {
        if (!e.data || e.data.surface !== surface) return;
        if (e.data.error) { (results.__errors = results.__errors || []).push([surface, e.data.error]); return; }
        clearTimeout(timer); window.removeEventListener('message', on); resolve(e.data.result);
      });
    });
    frame.src = '/surface/' + surface;
    document.body.appendChild(frame);
    results[surface] = await got;
    frame.remove();
    fetch('/progress', { method: 'POST', body: JSON.stringify(results) });
  }
  await fetch('/report', { method: 'POST', body: JSON.stringify(results) });
})();
</script></body></html>"""


def _base_scripts() -> str:
    """The scripts base.html gives every page: its own files and any inline renderer."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    parts = []
    for match in re.finditer(
        r'<script src="(/static/[^"]+)"></script>|<script>(.*?)</script>', base, re.S
    ):
        if match.group(1):
            parts.append(f'<script src="{match.group(1)}"></script>')
        elif "window.SparkMarkdown" in match.group(2):
            parts.append(match.group(0))
    return "\n".join(parts)


def _scripts_block(name: str) -> str:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    match = re.search(r"\{% block scripts %\}(.*?)\{% endblock %\}", source, re.S)
    assert match is not None, name
    return match.group(1)


def _inline(kind: str) -> str:
    if kind == "skills":
        return _scripts_block("skills.html")
    if kind == "help":
        topics = [{"id": "report", "title": "Report", "content": REPLY}]
        return (
            Environment(autoescape=False)
            .from_string(_scripts_block("help.html"))
            .render(topics=topics)
        )
    if kind == "actions":
        source = (TEMPLATES / "actions.html").read_text(encoding="utf-8")
        escape = re.search(r"function escapeHtml\(t\) \{.*?\}\n", source)
        assert escape is not None
        return f"<script>{escape.group(0)}{_function(source, '_renderMd')}</script>"
    if kind == "action_bubble":
        source = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
        return f"<script>{_function(source, '_appendActionBubble')}</script>"
    raise AssertionError(kind)


def surface_page(name: str) -> bytes:
    spec = PAGES[name]
    # "</" is written as "<\/" so the reply's own </script> cannot end the harness script.
    checks = CHECKS.replace("__REPLY__", json.dumps(REPLY).replace("</", "<\\/")).replace(
        "__SURFACE__", json.dumps(name)
    )
    scripts = "".join(
        f'<script src="/static/js/{s}"></script>' for s in spec.get("scripts", "").split()
    )
    inline = _inline(spec["inline"]) if "inline" in spec else ""
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{name}</title></head>
<body>{spec["body"]}
{PRELUDE}
{checks}
<script>{spec.get("before", "")}</script>
{_base_scripts()}
{scripts}
{inline}
<script>
window.addEventListener('load', async () => {{
  await pause(100);
  {spec["run"]}
}});
</script>
</body></html>"""
    return page.encode("utf-8")


class Harness:
    def __init__(self) -> None:
        top = (
            TOP.replace("__SURFACES__", json.dumps(list(PAGES)))
            .replace("__BUDGET__", str(SURFACE_BUDGET_MS))
            .encode("utf-8")
        )
        pages = {name: surface_page(name) for name in PAGES}
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        harness = self

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
                if self.path in ("/progress", "/report"):
                    harness.report = json.loads(body)
                if self.path == "/report":
                    harness.reported.set()
                self._send(204, b"", "text/plain")

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/harness":
                    self._send(200, top, "text/html; charset=utf-8")
                elif path.startswith("/surface/") and path[len("/surface/") :] in pages:
                    self._send(200, pages[path[len("/surface/") :]], "text/html; charset=utf-8")
                elif path.startswith("/static/"):
                    target = (STATIC / path[len("/static/") :]).resolve()
                    if not target.is_file() or STATIC.resolve() not in target.parents:
                        self._send(404, b"", "text/plain")
                    else:
                        self._send(200, target.read_bytes(), "text/javascript")
                else:
                    self._send(404, b"", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def run(self) -> dict[str, Any]:
        import subprocess

        assert BROWSER is not None
        with tempfile.TemporaryDirectory(prefix="spark-md-", ignore_cleanup_errors=True) as profile:
            browser = subprocess.Popen(
                [
                    BROWSER,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1200,900",
                    f"--user-data-dir={profile}",
                    f"http://127.0.0.1:{self.port}/harness",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = self.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, (
            f"the harness did not finish in {RUN_BUDGET_S} s; surfaces that had reported: "
            f"{sorted(self.report)}"
        )
        return self.report

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def results() -> Iterator[dict[str, Any]]:
    harness = Harness()
    try:
        yield harness.run()
    finally:
        harness.close()


browser = pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed")
MARKDOWN_SURFACES = [name for name in PAGES if name != "skills"]


@browser
@pytest.mark.timeout(RUN_BUDGET_S + 60)
class TestInABrowser:
    def test_no_page_raised_an_error(self, results: dict[str, Any]) -> None:
        assert results.get("__errors", []) == []
        assert [name for name in PAGES if "timeout" in results[name]] == []

    @pytest.mark.parametrize("surface", list(PAGES))
    def test_every_payload_is_removed_and_none_ran(
        self, results: dict[str, Any], surface: str
    ) -> None:
        got = results[surface]
        assert got["bad"] == []
        assert got["pwned"] == []
        assert "Safe" in got["text"] and "End of report." in got["text"]

    @pytest.mark.parametrize("surface", ["chat", "live"])
    def test_every_partial_render_of_a_stream_is_sanitised(
        self, results: dict[str, Any], surface: str
    ) -> None:
        assert results[surface]["stepBad"] == []

    @pytest.mark.parametrize("surface", MARKDOWN_SURFACES)
    def test_markdown_still_looks_as_it_did(self, results: dict[str, Any], surface: str) -> None:
        got = results[surface]
        assert got["h2"] >= 1 and got["tables"] >= 1
        assert got["code"] >= 2 and got["copy"] == got["code"]
        assert got["mermaid"] >= 1
        assert got["ticks"] >= 2 and got["ticks"] == got["ticksDisabled"]

    @pytest.mark.parametrize("surface", MARKDOWN_SURFACES)
    def test_links_open_in_a_new_tab_without_an_opener(
        self, results: dict[str, Any], surface: str
    ) -> None:
        links = results[surface]["links"]
        site = [link for link in links if link["href"] == "https://example.com/page"]
        assert site and all(
            link["target"] == "_blank" and link["rel"] == "noopener noreferrer" for link in site
        )
        assert {"href": "#notes", "target": None, "rel": None} in links
        assert all(not (link["href"] or "").lower().startswith("javascript") for link in links)

    @pytest.mark.parametrize("surface", MARKDOWN_SURFACES)
    def test_the_copy_button_copies_through_the_delegated_listener(
        self, results: dict[str, Any], surface: str
    ) -> None:
        got = results[surface]
        assert got["copied"] == ['print("hello")\n'] or got["copied"] == ['print("hello")']
        assert "bi-check-lg" in got["icon"]

    def test_a_stream_drawn_block_by_block_matches_the_whole_reply(
        self, results: dict[str, Any]
    ) -> None:
        assert results["chat"]["differ"] == []

    def test_code_is_highlighted_and_mermaid_drawn_in_chat(self, results: dict[str, Any]) -> None:
        got = results["chat"]
        assert got["hljs"] == got["code"] == 2 * got["bubbles"]
        assert got["mermaidProcessed"] == got["mermaid"] == got["bubbles"]
        assert got["mermaidConfig"]["securityLevel"] == "strict"
        # mermaid gets the diagram's source as text, exactly as written
        assert got["mermaidSources"] and all(
            "A[\"<img src=x onerror=pwn('mermaid')>\"] --> B" in s for s in got["mermaidSources"]
        )

    def test_the_skills_viewer_shows_the_source_as_text(self, results: dict[str, Any]) -> None:
        got = results["skills"]
        assert "<script>pwn('script')</script>" in got["text"]
