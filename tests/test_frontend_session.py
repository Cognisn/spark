"""The front end refreshes a server-mode session transparently (``static/js/csrf.js``).

When a same-origin request answers 401 with ``X-Spark-Session: refresh``, the
wrapper posts once to the refresh path (with the CSRF token), then retries the
request. Refreshes are serialised: requests failing together share one
refresh. A refused refresh, or ``X-Spark-Session: sign-in``, sends the page to
the sign-in screen. Other origins and other answers are left alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CSRF_JS = (
    Path(__file__).resolve().parents[1] / "src" / "spark" / "web" / "static" / "js" / "csrf.js"
)

_HARNESS = r"""
const calls = [];
let refreshAnswer = 204;
let expired = true;
let refreshes = 0;
const visited = [];
globalThis.location = {
  href: 'https://spark.example.com/chat/1?x=1', origin: 'https://spark.example.com',
  pathname: '/chat/1', search: '?x=1', assign: (url) => visited.push(url),
};
globalThis.document = {
  querySelector: (sel) => sel === 'meta[name="csrf-token"]'
    ? { getAttribute: () => 'synthetic-token' } : null,
};
globalThis.window = globalThis;
function answer(status, header) {
  return { status, ok: status >= 200 && status < 300,
           headers: new Headers(header ? { 'X-Spark-Session': header } : {}) };
}
globalThis.fetch = (input, init) => {
  const url = String(input instanceof Request ? input.url : input);
  const method = String((init && init.method) || 'GET').toUpperCase();
  calls.push({ url, method, csrf: new Headers((init && init.headers) || {}).get('X-CSRF-Token') });
  if (url.endsWith('/auth/session/refresh')) {
    refreshes += 1;
    return new Promise((resolve) => setTimeout(() => {
      if (refreshAnswer === 204) expired = false;
      resolve(answer(refreshAnswer, refreshAnswer === 204 ? null : 'sign-in'));
    }, 5));
  }
  if (url.includes('example.org')) return Promise.resolve(answer(401, 'refresh'));
  if (url.endsWith('/gone')) return Promise.resolve(answer(401, 'sign-in'));
  if (url.endsWith('/forbidden')) return Promise.resolve(answer(403));
  return Promise.resolve(expired ? answer(401, 'refresh') : answer(200));
};
require(process.argv[2]);

(async () => {
  const out = {};
  let r = await fetch('/api/x');
  out.single = { status: r.status, refreshes, calls: calls.map(c => [c.method, c.url, c.csrf]) };

  expired = true; refreshes = 0; calls.length = 0;
  const all = await Promise.all([fetch('/api/a'), fetch('/api/b', { method: 'POST' }), fetch('/api/c')]);
  out.together = { statuses: all.map(x => x.status), refreshes };

  expired = true; refreshes = 0; refreshAnswer = 401; calls.length = 0;
  r = await fetch('/api/y');
  out.refused = { status: r.status, refreshes, visited: visited.slice() };

  visited.length = 0; refreshes = 0; refreshAnswer = 204;
  r = await fetch('https://example.org/api');
  out.cross = { status: r.status, refreshes };

  r = await fetch('/forbidden');
  out.forbidden = { status: r.status, refreshes };

  r = await fetch('/gone');
  out.gone = { status: r.status, refreshes, visited: visited.slice() };

  expired = true; refreshes = 0;
  r = await window.SparkSession.ensure();
  out.ensure = { refreshes, ok: r };
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    harness = tmp_path_factory.mktemp("session") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8", newline="\n")
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
class TestRefresh:
    def test_one_refresh_then_the_request_is_retried(self, results: dict[str, object]) -> None:
        single = results["single"]
        assert isinstance(single, dict)
        assert single["status"] == 200
        assert single["refreshes"] == 1
        assert single["calls"] == [
            ["GET", "/api/x", None],
            ["POST", "/auth/session/refresh", "synthetic-token"],
            ["GET", "/api/x", None],
        ]

    def test_requests_failing_together_share_one_refresh(self, results: dict[str, object]) -> None:
        assert results["together"] == {"statuses": [200, 200, 200], "refreshes": 1}

    def test_a_refused_refresh_goes_to_sign_in(self, results: dict[str, object]) -> None:
        assert results["refused"] == {
            "status": 401,
            "refreshes": 1,
            "visited": ["/auth/sign-in?next=%2Fchat%2F1%3Fx%3D1"],
        }

    def test_other_origins_are_never_refreshed(self, results: dict[str, object]) -> None:
        assert results["cross"] == {"status": 401, "refreshes": 0}

    def test_other_answers_are_left_alone(self, results: dict[str, object]) -> None:
        assert results["forbidden"] == {"status": 403, "refreshes": 0}

    def test_sign_in_answer_goes_to_sign_in(self, results: dict[str, object]) -> None:
        assert results["gone"] == {
            "status": 401,
            "refreshes": 0,
            "visited": ["/auth/sign-in?next=%2Fchat%2F1%3Fx%3D1"],
        }

    def test_ensure_refreshes_before_a_stream_opens(self, results: dict[str, object]) -> None:
        assert results["ensure"] == {"refreshes": 1, "ok": True}
