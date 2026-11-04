"""The chat composer's attach control (``static/js/attachments.js``).

Under Node, with ``csrf.js`` loaded first as on every page, the control:

* uploads each file to ``/files/api`` with the conversation id through the
  page's fetch, so the upload carries the session's CSRF token;
* refuses a file over the upload limit before sending it, and shows the upload
  limit and quota refusals (413, 507) as plain messages naming the file;
* reads back the note the send routes add (``spark.web.attachments``) into
  the message text and its files, so a message shows its attachments as chips,
  with names escaped.

The template checks pin the control into the chat page.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from spark.web.attachments import MARKER, with_attachments

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
STATIC_JS = WEB / "static" / "js"
CSRF_JS = STATIC_JS / "csrf.js"
ATTACH_JS = STATIC_JS / "attachments.js"
CHAT_HTML = WEB / "templates" / "chat.html"
MB = 1024 * 1024

_HARNESS = r"""
const calls = [];
const replies = [];
globalThis.location = { href: 'http://127.0.0.1:23100/chat/7', origin: 'http://127.0.0.1:23100' };
globalThis.document = {
  querySelector: (sel) => sel === 'meta[name="csrf-token"]'
    ? { getAttribute: () => 'synthetic-token' } : null,
};
globalThis.window = globalThis;
globalThis.fetch = (input, init) => {
  calls.push({ input: String(input), init: init || {} });
  const next = replies.shift() || { status: 500, body: {} };
  if (next.throws) return Promise.reject(new Error(next.throws));
  return Promise.resolve(new Response(JSON.stringify(next.body), { status: next.status }));
};
require(process.argv[2]);
require(process.argv[3]);
const cases = JSON.parse(process.argv[4]);
const A = window.SparkAttachments;
const LIMITS = { max_upload_bytes: 50 * 1024 * 1024, quota_bytes: 1024 * 1024 * 1024, usage_bytes: 0 };

function file(name, size) {
  return new File([new Uint8Array(size)], name, { type: 'text/plain' });
}

(async () => {
  const out = {};

  replies.push({ status: 201, body: { file: { id: 41, filename: 'budget.txt', size_bytes: 12 } } });
  out.ok = await A.upload(7, file('budget.txt', 12), LIMITS);
  const sent = calls.pop();
  const headers = new Headers(sent.init.headers);
  out.request = {
    url: sent.input,
    method: sent.init.method,
    token: headers.get('X-CSRF-Token'),
    conversation: sent.init.body.get('conversation_id'),
    filename: sent.init.body.get('file').name,
  };

  replies.push({ status: 413, body: { error: 'The file is larger than the maximum upload size of 52,428,800 bytes.' } });
  out.tooLarge = await A.upload(7, file('big.pdf', 10), LIMITS);
  calls.pop();

  replies.push({ status: 507, body: { error: 'Storage quota exceeded: 1,000 of 2,048 bytes are in use and 2,000 more were requested.' } });
  out.quota = await A.upload(7, file('more.txt', 10), { max_upload_bytes: null, quota_bytes: 2048, usage_bytes: 1000 });
  calls.pop();

  const before = calls.length;
  out.precheck = await A.upload(7, file('huge.bin', 2048), { max_upload_bytes: 1024, quota_bytes: null, usage_bytes: 0 });
  out.precheckCalls = calls.length - before;

  replies.push({ status: 403, body: { error: 'CSRF token missing or invalid' } });
  out.csrf = await A.upload(7, file('a.txt', 1), LIMITS);
  calls.pop();

  replies.push({ status: 503, body: { error: 'File storage is not available: x.' } });
  out.unavailable = await A.upload(7, file('a.txt', 1), LIMITS);
  calls.pop();

  replies.push({ throws: 'offline' });
  out.network = await A.upload(7, file('a.txt', 1), LIMITS);
  calls.pop();

  replies.push({ status: 200, body: { files: [], usage_bytes: 5, quota_bytes: 10, max_upload_bytes: 20 } });
  out.limits = await A.limits();
  out.limitsRequest = calls.pop().input;

  out.parsed = cases.map((c) => A.parse(c));
  out.chips = A.chipsHtml([{ id: 3, name: '<img src=x onerror=alert(1)>.txt' }]);
  out.size = [A.formatBytes(512), A.formatBytes(1536), A.formatBytes(50 * 1024 * 1024)];
  console.log(JSON.stringify(out));
})();
"""


def _cases() -> list[str]:
    return [
        with_attachments(
            "Please summarise.",
            [{"id": 3, "filename": "a.txt"}, {"id": 5, "filename": "b (final).pdf"}],
        ),
        with_attachments("", [{"id": 7, "filename": "c.txt"}]),
        "plain words",
        f"{MARKER}\n- file 1: x.txt\nthen more words",
        f"Quoted:\n{MARKER}\n- file 1: x.txt\n\nand a reply after it",
    ]


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    harness = tmp_path_factory.mktemp("attach") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8", newline="\n")
    done = subprocess.run(
        ["node", str(harness), str(CSRF_JS), str(ATTACH_JS), json.dumps(_cases())],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    loaded: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return loaded


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
class TestUnderNode:
    def test_upload_posts_the_file_with_the_conversation_and_the_csrf_token(
        self, results: dict[str, Any]
    ) -> None:
        assert results["request"] == {
            "url": "/files/api",
            "method": "POST",
            "token": "synthetic-token",
            "conversation": "7",
            "filename": "budget.txt",
        }
        assert results["ok"] == {
            "ok": True,
            "file": {"id": 41, "name": "budget.txt", "size": 12},
        }

    def test_the_upload_limit_refusal_names_the_file_and_the_limit(
        self, results: dict[str, Any]
    ) -> None:
        assert results["tooLarge"]["ok"] is False
        message = results["tooLarge"]["message"]
        assert message.startswith("big.pdf was not attached")
        assert "upload limit" in message and "50 MB" in message

    def test_the_quota_refusal_says_storage_is_full(self, results: dict[str, Any]) -> None:
        message = results["quota"]["message"]
        assert message.startswith("more.txt was not attached")
        assert "storage is full" in message
        assert "1000 B of 2.0 KB" in message

    def test_a_file_over_the_limit_is_refused_before_it_is_sent(
        self, results: dict[str, Any]
    ) -> None:
        assert results["precheckCalls"] == 0
        message = results["precheck"]["message"]
        assert message.startswith("huge.bin was not attached")
        assert "upload limit" in message and "1.0 KB" in message

    def test_other_refusals_are_plain(self, results: dict[str, Any]) -> None:
        assert "reload the page" in results["csrf"]["message"].lower()
        assert "not available" in results["unavailable"]["message"]
        assert "offline" in results["network"]["message"]
        for key in ("csrf", "unavailable", "network"):
            assert results[key]["ok"] is False

    def test_limits_come_from_the_files_listing(self, results: dict[str, Any]) -> None:
        assert results["limitsRequest"] == "/files/api"
        assert results["limits"] == {"usage_bytes": 5, "quota_bytes": 10, "max_upload_bytes": 20}

    def test_reads_back_the_note_the_send_routes_write(self, results: dict[str, Any]) -> None:
        both, alone, plain, trailing, quoted = results["parsed"]
        assert both == {
            "text": "Please summarise.",
            "files": [{"id": 3, "name": "a.txt"}, {"id": 5, "name": "b (final).pdf"}],
        }
        assert alone == {"text": "", "files": [{"id": 7, "name": "c.txt"}]}
        assert plain == {"text": "plain words", "files": []}
        assert trailing["files"] == [] and quoted["files"] == []

    def test_chip_names_are_escaped_and_link_to_the_download(self, results: dict[str, Any]) -> None:
        chips = results["chips"]
        assert "<img" not in chips
        assert "&lt;img src=x onerror=alert(1)&gt;.txt" in chips
        assert 'href="/files/api/3"' in chips

    def test_sizes_read_naturally(self, results: dict[str, Any]) -> None:
        assert results["size"] == ["512 B", "1.5 KB", "50 MB"]


class TestChatPage:
    def test_the_page_loads_the_control_and_has_an_attach_button(self) -> None:
        text = CHAT_HTML.read_text(encoding="utf-8")
        assert '<script src="/static/js/attachments.js"></script>' in text
        assert text.index("/static/js/attachments.js") < text.index("/static/js/chat.js")
        assert 'id="btn-attach"' in text
        assert 'id="attach-input"' in text and 'type="file"' in text
        assert 'id="attach-pending"' in text and 'id="attach-errors"' in text

    def test_the_control_uploads_only_through_the_wrapped_fetch(self) -> None:
        source = ATTACH_JS.read_text(encoding="utf-8")
        assert "XMLHttpRequest" not in source
        assert "sendBeacon" not in source
        assert "fetch('/files/api'" in source

    def test_the_stream_carries_the_attachment_ids(self) -> None:
        source = (STATIC_JS / "sse-client.js").read_text(encoding="utf-8")
        assert "attachments" in source
