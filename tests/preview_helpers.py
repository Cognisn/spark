"""Test helpers for the document preview: a stub link server and synthetic documents.

:class:`LinkServer` stands in for KVASIR's document links (feat/0.3.0-document-links):
a local HTTP server serving fixtures with the headers those links send (the content
type, ``X-Content-Type-Options: nosniff``, ``Content-Disposition`` inline or attachment,
``Accept-Ranges`` and single ranges), plus the misbehaviours the fetch guard refuses
(a redirect to another host, a slow answer, an oversize answer without a length, a
type that does not match the content). Every request's headers are kept, so a test
can show what the guard sent.

The documents are synthetic, made here: no real document is used.
"""

from __future__ import annotations

import io
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from spark.preview.guard import McpOrigin

__all__ = [
    "Fixture",
    "LinkServer",
    "csv_bytes",
    "docx_bytes",
    "markdown_bytes",
    "origin_for",
    "pdf_bytes",
    "png_bytes",
    "svg_bytes",
    "text_bytes",
    "xlsx_bytes",
    "zip_bytes",
]


def pdf_bytes(pages: int = 2) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    out = io.BytesIO()
    drawing = canvas.Canvas(out, pagesize=A4)
    for number in range(1, pages + 1):
        drawing.setFont("Helvetica", 18)
        drawing.drawString(72, 760, f"Synthetic report, page {number}")
        drawing.setFont("Helvetica", 11)
        drawing.drawString(72, 730, "Prepared for Example Organisation (example.org).")
        drawing.showPage()
    drawing.save()
    return out.getvalue()


def docx_bytes() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Synthetic briefing", level=1)
    document.add_paragraph("This document was generated for the preview tests.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Item"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Widgets"
    table.cell(1, 1).text = "42"
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    first = book.active
    first.title = "Budget"
    first.append(["Quarter", "Spend"])
    for quarter, spend in (("Q1", 1200), ("Q2", 1350), ("Q3", 990)):
        first.append([quarter, spend])
    second = book.create_sheet("People")
    second.append(["Name", "Team"])
    second.append(["Ada Example", "Research"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def csv_bytes() -> bytes:
    return b"host,owner\nalpha.example.net,Team A\nbeta.example.net,Team B\n"


def png_bytes(width: int = 8, height: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(b"\x00" + b"\x20\x60\xc0" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def svg_bytes() -> bytes:
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        b'<rect width="40" height="20" fill="#0b6bcb"/>'
        b"<script>window.parent.postMessage('svg script ran','*')</script></svg>"
    )


def markdown_bytes() -> bytes:
    return (
        b"# Synthetic notes\n\nA list:\n\n- one\n- two\n\n"
        b"<img src=x onerror=\"window.parent.postMessage('markdown script ran','*')\">\n\n"
        b"[a link](https://example.com/)\n"
    )


def text_bytes() -> bytes:
    return b"def hello():\n    return 'world'\n\nprint(hello())\n"


def zip_bytes() -> bytes:
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("readme.txt", "synthetic")
    return out.getvalue()


@dataclass
class Fixture:
    """One answer of the stub link server."""

    body: bytes = b""
    content_type: str = "application/octet-stream"
    disposition: str | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    send_length: bool = True
    # Send the body in this many parts, `drip` seconds apart (a slow answer).
    parts: int = 1
    drip: float = 0.0


class LinkServer:
    """A local stand-in for a KVASIR document link host, on 127.0.0.1."""

    def __init__(self) -> None:
        self.fixtures: dict[str, Fixture] = {}
        self.requests: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # quiet
                return None

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                server.requests.append({"path": self.path, "headers": dict(self.headers)})
                fixture = server.fixtures.get(self.path.split("?", 1)[0])
                if fixture is None:
                    self._answer(Fixture(b"not found", "text/plain", status=404))
                    return
                if fixture.delay:
                    time.sleep(fixture.delay)
                self._answer(fixture)

            def _answer(self, fixture: Fixture) -> None:
                body = fixture.body
                status = fixture.status
                extra: dict[str, str] = {}
                wanted = self.headers.get("Range", "")
                if status == 200 and wanted.startswith("bytes=") and "," not in wanted:
                    start_text, _, end_text = wanted[6:].partition("-")
                    start = int(start_text or 0)
                    end = int(end_text) if end_text else len(body) - 1
                    extra["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                    body, status = body[start : end + 1], 206
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", fixture.content_type)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "private, no-store")
                    if fixture.disposition:
                        self.send_header("Content-Disposition", fixture.disposition)
                    for name, value in {**fixture.headers, **extra}.items():
                        self.send_header(name, value)
                    if fixture.send_length:
                        self.send_header("Content-Length", str(len(body)))
                    else:
                        self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    step = max(1, -(-len(body) // max(fixture.parts, 1)))
                    for start in range(0, len(body), step):
                        if fixture.drip:
                            time.sleep(fixture.drip)
                        self.wfile.write(body[start : start + step])
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                if not fixture.send_length:
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> LinkServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add(self, path: str, fixture: Fixture) -> str:
        """Serve ``fixture`` at ``path``; returns the link (http://127.0.0.1:<port><path>)."""
        self.fixtures[path] = fixture
        return self.url(path)

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


def origin_for(server: LinkServer, host: str = "127.0.0.1", **overrides: Any) -> McpOrigin:
    """The MCP origin of a server at the link server's address (http, its port)."""
    values: dict[str, Any] = {
        "name": "kvasir",
        "scheme": "http",
        "host": host,
        "port": server.port,
        "addresses": frozenset({"127.0.0.1"}),
    }
    values.update(overrides)
    return McpOrigin(**values)
