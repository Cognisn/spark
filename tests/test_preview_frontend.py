"""The preview's pages and assets ask no CDN for anything; Markdown is self-hosted and sanitised.

* No request to any CDN: the preview's templates, scripts and styles, and every vendored
  library, name no content delivery network; no template loads marked (or any of the
  preview's libraries) from one; base.html loads marked and DOMPurify from
  ``static/vendor``.
* The vendored files are the pinned ones (the licence audit's checksums).
* The pane loads each document in ``<iframe sandbox="allow-scripts">`` and never grants
  the frame SPARK's origin.
* The release notes render through the sanitised renderer, in a real browser.
* A viewer following another member's turn gets the shown document's card with the
  live tool event, and nothing else of the result.
"""

from __future__ import annotations

import html
import importlib.util
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from spark.preview.service import Card, card_line
from spark.web.endpoints.streaming import RunningTurn
from tests.test_preview_renderers import BROWSER, run_browser

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "spark" / "web"
TEMPLATES = WEB / "templates"
STATIC = WEB / "static"
VENDOR = STATIC / "vendor"

CDN_HOSTS = re.compile(
    r"cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|cdn\.sheetjs\.com|esm\.sh|"
    r"skypack\.dev|jspm\.io|googleapis\.com|gstatic\.com|cloudfront\.net|cdn\.",
    re.I,
)
PREVIEW_FILES = [
    TEMPLATES / "preview_frame.html",
    TEMPLATES / "_preview_pane.html",
    STATIC / "js" / "preview.js",
    STATIC / "js" / "preview_frame.js",
    STATIC / "css" / "preview.css",
]
LIBRARIES = ("marked", "purify", "dompurify", "pdf.js", "pdfjs", "xlsx", "sheetjs", "docx", "jszip")


def _audit() -> Any:
    import sys

    if "licence_audit" in sys.modules:
        return sys.modules["licence_audit"]
    spec = importlib.util.spec_from_file_location(
        "licence_audit", ROOT / "scripts" / "licence_audit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["licence_audit"] = module
    spec.loader.exec_module(module)
    return module


class TestNoCdn:
    def test_the_preview_names_no_external_host(self) -> None:
        for path in PREVIEW_FILES:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", text), path.name
            assert not re.search(r"""url\(\s*["']?(https?:)?//""", text), path.name
            assert not re.search(r"""(import|fetch)\s*\(\s*["'`](https?:)?//""", text), path.name
            assert not CDN_HOSTS.search(text), path.name

    def test_no_vendored_library_asks_a_cdn(self) -> None:
        files = [p for p in VENDOR.rglob("*") if p.suffix in (".js", ".mjs")]
        assert len(files) == 7
        for path in files:
            assert not CDN_HOSTS.search(path.read_text(encoding="utf-8")), path.name

    def test_no_template_loads_a_preview_library_from_a_cdn(self) -> None:
        for path in TEMPLATES.glob("*.html"):
            for tag in re.findall(r"<(?:script|link)\b[^>]*>", path.read_text(encoding="utf-8")):
                if CDN_HOSTS.search(tag):
                    assert not any(lib in tag.lower() for lib in LIBRARIES), (path.name, tag)

    def test_base_loads_markdown_from_spark(self) -> None:
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        assert '<script src="/static/vendor/marked/marked.umd.js"></script>' in base
        assert '<script src="/static/vendor/dompurify/purify.min.js"></script>' in base
        assert "SparkMarkdown.safe(_updateData.release_notes)" in base
        assert "marked.parse(_updateData.release_notes)" not in base

    def test_every_vendored_file_is_pinned(self) -> None:
        audit = _audit()
        pinned = {
            Path(f).name for asset in audit.BUNDLED_ASSETS.values() for f in asset.get("sha256", {})
        }
        shipped = {p.name for p in VENDOR.rglob("*") if p.suffix in (".js", ".mjs")}
        assert shipped == pinned
        assert audit.check_bundled_assets() == []

    def test_a_changed_vendored_file_fails_the_audit(self, tmp_path: Path) -> None:
        audit = _audit()
        (tmp_path / "lib.js").write_text("changed", encoding="utf-8")
        (tmp_path / "LICENSE.txt").write_text("MIT", encoding="utf-8")
        (tmp_path / "THIRD-PARTY-NOTICES.md").write_text("Lib 1.0 MIT\n", encoding="utf-8")
        assets = {
            "Lib": {
                "version": "1.0",
                "licence": "MIT",
                "licence_file": "LICENSE.txt",
                "files": ["lib.js"],
                "sha256": {"lib.js": "0" * 64},
            }
        }
        assert any("pinned checksum" in p for p in audit.check_bundled_assets(assets, tmp_path))
        del assets["Lib"]["sha256"]
        assert any("no pinned checksum" in p for p in audit.check_bundled_assets(assets, tmp_path))
        (tmp_path / "THIRD-PARTY-NOTICES.md").write_text("Lib MIT\n", encoding="utf-8")
        assets["Lib"]["sha256"] = {"lib.js": "0" * 64}
        assert any("version 1.0" in p for p in audit.check_bundled_assets(assets, tmp_path))


class TestSandbox:
    def test_each_document_is_in_a_scripts_only_sandbox(self) -> None:
        pane = (STATIC / "js" / "preview.js").read_text(encoding="utf-8")
        assert "frame.setAttribute('sandbox', 'allow-scripts');" in pane
        for grant in ("allow-same-origin", "allow-top-navigation", "allow-popups", "allow-forms"):
            assert grant not in pane

    def test_the_frame_runs_spark_scripts_only(self) -> None:
        frame = (TEMPLATES / "preview_frame.html").read_text(encoding="utf-8")
        assert re.findall(r"<script[^>]*>", frame) == [
            '<script src="/static/vendor/dompurify/purify.min.js">',
            '<script src="/static/vendor/marked/marked.umd.js">',
            '<script src="/static/js/preview_frame.js">',
        ]
        assert "style=" not in frame

    def test_the_chat_page_holds_the_pane(self) -> None:
        chat = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
        assert '{% include "_preview_pane.html" %}' in chat
        assert chat.index("/static/js/preview.js") < chat.index("/static/js/chat.js")


class TestLiveViewers:
    def _observe(self, data: dict[str, Any], conversation_id: int = 4) -> list[Any]:
        turn = SimpleNamespace(_lock=threading.Lock(), _events=[], conversation_id=conversation_id)
        RunningTurn.observe(turn, "tool_result", data)  # type: ignore[arg-type]
        return turn._events

    def test_the_card_travels_with_the_tool_event(self) -> None:
        card = Card(9, 4, "a.pdf", "A", "application/pdf", "pdf", 10)
        events = self._observe(
            {"tool_name": "show_document", "status": "success", "result": f"x\n{card_line(card)}"}
        )
        assert events == [
            (
                "tool",
                {
                    "phase": "end",
                    "tool_name": "show_document",
                    "card": {
                        "file_id": 9,
                        "conversation_id": 4,
                        "name": "a.pdf",
                        "title": "A",
                        "type": "application/pdf",
                        "kind": "pdf",
                        "size": 10,
                    },
                },
            )
        ]

    def test_nothing_else_of_a_result_travels(self) -> None:
        card = Card(9, 5, "a.pdf", "A", "application/pdf", "pdf", 10)
        line = card_line(card)
        for data in (
            {"tool_name": "show_document", "status": "error", "result": line},
            {"tool_name": "read_my_file", "status": "success", "result": line},
            {
                "tool_name": "show_document",
                "status": "success",
                "result": line,
            },  # other conversation
        ):
            assert self._observe(data) == [
                ("tool", {"phase": "end", "tool_name": data["tool_name"]})
            ]


RELEASE_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div id="notes"></div><pre id="out">pending</pre>
<script src="/static/vendor/marked/marked.umd.js"></script>
<script src="/static/vendor/dompurify/purify.min.js"></script>
{renderer}
<script>
const notes = "## What's new\\n\\n- Document preview\\n- **Faster** sign-in\\n\\n" +
  "<img src=x onerror=\\"document.title='pwned'\\"><script>document.title='pwned'<\\/script>";
const el = document.getElementById('notes');
el.innerHTML = SparkMarkdown.safe(notes);
document.getElementById('out').textContent = JSON.stringify({{
  h2: el.querySelectorAll('h2').length, items: el.querySelectorAll('li').length,
  strong: el.querySelectorAll('strong').length, handlers: el.querySelectorAll('[onerror]').length,
  scripts: el.querySelectorAll('script').length, title: document.title }});
</script></body></html>"""


@pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed")
def test_the_release_notes_render_sanitised_in_a_browser() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    renderer = '<script src="/static/js/markdown.js"></script>'
    assert renderer in base
    page = RELEASE_PAGE.format(renderer=renderer).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/static/"):
                body = (STATIC / self.path[len("/static/") :]).read_bytes()
                kind = "text/javascript"
            else:
                body, kind = page, "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dom = run_browser(f"http://127.0.0.1:{server.server_address[1]}/", "--dump-dom")
    finally:
        server.shutdown()
        server.server_close()
    found = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    assert found is not None
    import json

    result = json.loads(html.unescape(found.group(1)))
    assert result == {"h2": 1, "items": 2, "strong": 1, "handlers": 0, "scripts": 0, "title": ""}
