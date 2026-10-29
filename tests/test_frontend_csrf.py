"""The front end sends the CSRF token on every state-changing request.

Pages load ``/static/js/csrf.js`` first, which adds the token from the page's
``csrf-token`` meta tag to every same-origin POST, PUT, PATCH and DELETE made
with ``fetch``. These tests check that every page that can make such a request
loads it, that nothing bypasses ``fetch``, that every HTML form posting to the
server carries the token, and (under Node) that the wrapper does what it says.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
TEMPLATES = WEB / "templates"
STATIC_JS = WEB / "static" / "js"
CSRF_JS = STATIC_JS / "csrf.js"

META = '<meta name="csrf-token" content="{{ csrf_token(request) }}">'
SCRIPT = '<script src="/static/js/csrf.js"></script>'
# Pages rendered without base.html, and why each is safe.
STANDALONE = {"base.html", "login.html", "loading.html"}

UNSAFE_METHOD = re.compile(r"""method\s*:\s*['"`](POST|PUT|PATCH|DELETE)['"`]""", re.I)


def _templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def _sources() -> list[Path]:
    return _templates() + sorted(STATIC_JS.glob("*.js"))


class TestPagesLoadTheWrapper:
    @pytest.mark.parametrize("name", ["base.html", "loading.html"])
    def test_wrapper_and_token_come_before_any_page_script(self, name: str) -> None:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert META in text and SCRIPT in text
        script_at = text.index(SCRIPT)
        assert text.index(META) < script_at
        for match in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.S):
            if "data-bs-theme" in match.group(1):
                continue  # the one-line theme script makes no requests
            assert match.start() >= script_at, f"a script in {name} runs before csrf.js"
        if "{% block head %}" in text:
            assert script_at < text.index("{% block head %}")

    @pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
    def test_every_other_page_extends_base(self, path: Path) -> None:
        if path.name in STANDALONE:
            return
        assert path.read_text(encoding="utf-8").lstrip().startswith('{% extends "base.html" %}')

    def test_login_page_makes_no_scripted_state_changes(self) -> None:
        text = (TEMPLATES / "login.html").read_text(encoding="utf-8")
        assert not UNSAFE_METHOD.search(text)


class TestNothingBypassesFetch:
    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_no_other_request_api(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for api in ("XMLHttpRequest", "sendBeacon", "$.ajax", "$.post", "axios"):
            assert api not in text, f"{path.name} uses {api}"

    @pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
    def test_every_posting_form_carries_the_token(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for form in re.finditer(r"<form\b[^>]*>(.*?)</form>", text, re.S | re.I):
            opening = form.group(0)[: form.group(0).index(">") + 1].lower()
            if 'method="post"' not in opening:
                continue
            assert 'name="csrf_token"' in form.group(1), f"{path.name}: {opening}"

    def test_sign_out_is_a_posted_form(self) -> None:
        text = (TEMPLATES / "main_menu.html").read_text(encoding="utf-8")
        assert 'href="/logout"' not in text
        assert re.search(r'<form method="post" action="/logout"', text)

    def test_scripted_state_changes_exist_to_be_covered(self) -> None:
        # Guards the regex above from silently matching nothing.
        found = sum(len(UNSAFE_METHOD.findall(p.read_text(encoding="utf-8"))) for p in _sources())
        assert found > 40


_NODE_HARNESS = r"""
const calls = [];
const store = { token: 'synthetic-token' };
globalThis.location = { href: 'http://127.0.0.1:22400/chat/1', origin: 'http://127.0.0.1:22400' };
globalThis.document = {
  querySelector: (sel) => sel === 'meta[name="csrf-token"]'
    ? { getAttribute: () => store.token } : null,
};
globalThis.window = globalThis;
globalThis.fetch = (input, init) => { calls.push({ input, init }); return Promise.resolve('ok'); };
require(process.argv[2]);

function header(call) {
  const h = new Headers(call.init && call.init.headers ? call.init.headers : undefined);
  if (call.input instanceof Request) {
    for (const [k, v] of call.input.headers) if (!h.has(k)) h.set(k, v);
  }
  return h.get('X-CSRF-Token');
}

(async () => {
  const out = {};
  for (const m of ['POST', 'PUT', 'PATCH', 'DELETE', 'post']) {
    await fetch('/api/x', { method: m });
    out[m] = header(calls.pop());
  }
  await fetch('/api/x');
  out.GET = header(calls.pop());
  await fetch('/api/x', { method: 'GET' });
  out.GET_EXPLICIT = header(calls.pop());
  await fetch('https://example.com/api', { method: 'POST' });
  out.CROSS = header(calls.pop());
  await fetch('/api/x', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
  let c = calls.pop();
  out.OBJECT = [header(c), new Headers(c.init.headers).get('Content-Type')];
  await fetch('/api/x', { method: 'POST', headers: new Headers({ 'X-Other': '1' }) });
  c = calls.pop();
  out.HEADERS = [header(c), new Headers(c.init.headers).get('X-Other')];
  await fetch('/api/x', { method: 'DELETE', headers: [['X-Other', '2']] });
  c = calls.pop();
  out.ARRAY = [header(c), new Headers(c.init.headers).get('X-Other')];
  await fetch(new Request('http://127.0.0.1:22400/api/x', { method: 'PUT' }));
  out.REQUEST = header(calls.pop());
  await fetch(new URL('http://127.0.0.1:22400/api/x'), { method: 'PATCH' });
  out.URL = header(calls.pop());
  store.token = 'rotated';
  await fetch('/api/x', { method: 'POST' });
  out.LIVE = header(calls.pop());
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """What csrf.js did to each call in the harness, run once under Node."""
    harness = tmp_path_factory.mktemp("csrf") / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8", newline="\n")
    done = subprocess.run(
        ["node", str(harness), str(CSRF_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    loaded: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return loaded


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
class TestWrapperUnderNode:

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
    def test_state_changing_methods_carry_the_token(
        self, results: dict[str, object], method: str
    ) -> None:
        assert results[method] == "synthetic-token"

    def test_reads_leave_it_off(self, results: dict[str, object]) -> None:
        assert results["GET"] is None
        assert results["GET_EXPLICIT"] is None

    def test_other_origins_never_see_it(self, results: dict[str, object]) -> None:
        assert results["CROSS"] is None

    def test_existing_headers_are_kept(self, results: dict[str, object]) -> None:
        assert results["OBJECT"] == ["synthetic-token", "application/json"]
        assert results["HEADERS"] == ["synthetic-token", "1"]
        assert results["ARRAY"] == ["synthetic-token", "2"]

    def test_request_and_url_inputs_are_covered(self, results: dict[str, object]) -> None:
        assert results["REQUEST"] == "synthetic-token"
        assert results["URL"] == "synthetic-token"

    def test_token_is_read_at_call_time(self, results: dict[str, object]) -> None:
        assert results["LIVE"] == "rotated"
