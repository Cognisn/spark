"""What a previewed document is (spark.preview.kinds): its renderer, and its content check."""

from __future__ import annotations

import pytest

from spark.preview.kinds import Kind, classify, content_matches
from tests.preview_helpers import (
    csv_bytes,
    docx_bytes,
    markdown_bytes,
    pdf_bytes,
    png_bytes,
    svg_bytes,
    text_bytes,
    xlsx_bytes,
    zip_bytes,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLS_HEAD = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100


@pytest.mark.parametrize(
    ("declared", "name", "kind", "media"),
    [
        ("application/pdf", "a.pdf", Kind.PDF, "application/pdf"),
        (DOCX, "a.docx", Kind.DOCX, DOCX),
        (XLSX, "a.xlsx", Kind.SHEET, XLSX),
        ("application/vnd.ms-excel", "a.xls", Kind.SHEET, "application/vnd.ms-excel"),
        ("text/csv; charset=utf-8", "a.csv", Kind.SHEET, "text/csv"),
        ("image/png", "a.png", Kind.IMAGE, "image/png"),
        ("image/svg+xml", "a.svg", Kind.IMAGE, "image/svg+xml"),
        ("text/markdown", "a.md", Kind.MARKDOWN, "text/markdown"),
        ("text/plain", "a.txt", Kind.TEXT, "text/plain"),
        ("application/json", "a.json", Kind.TEXT, "application/json"),
        ("text/html", "a.html", Kind.TEXT, "text/html"),
        ("application/zip", "a.zip", Kind.OTHER, "application/zip"),
        ("application/octet-stream", "report.PDF", Kind.PDF, "application/pdf"),
        ("application/octet-stream", "script.py", Kind.TEXT, "text/plain"),
        ("application/octet-stream", "blob.bin", Kind.OTHER, "application/octet-stream"),
        ("", "a.xlsx", Kind.SHEET, XLSX),
        ("application/vnd.ms-powerpoint", "a.ppt", Kind.OTHER, "application/vnd.ms-powerpoint"),
    ],
)
def test_classify(declared: str, name: str, kind: Kind, media: str) -> None:
    assert classify(declared, name) == (kind, media)


@pytest.mark.parametrize(
    ("kind", "media", "content"),
    [
        (Kind.PDF, "application/pdf", pdf_bytes(1)),
        (Kind.DOCX, DOCX, docx_bytes()),
        (Kind.SHEET, XLSX, xlsx_bytes()),
        (Kind.SHEET, "application/vnd.ms-excel", XLS_HEAD),
        (Kind.SHEET, "text/csv", csv_bytes()),
        (Kind.IMAGE, "image/png", png_bytes()),
        (Kind.IMAGE, "image/svg+xml", svg_bytes()),
        (Kind.IMAGE, "image/jpeg", b"\xff\xd8\xff\xe0rest"),
        (Kind.IMAGE, "image/gif", b"GIF89a..."),
        (Kind.IMAGE, "image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
        (Kind.MARKDOWN, "text/markdown", markdown_bytes()),
        (Kind.TEXT, "text/plain", text_bytes()),
        (Kind.TEXT, "text/plain", "café".encode() + b"\xc3"),  # cut mid-character
        (Kind.OTHER, "application/zip", zip_bytes()),
    ],
)
def test_matching_content(kind: Kind, media: str, content: bytes) -> None:
    assert content_matches(kind, media, content)


@pytest.mark.parametrize(
    ("kind", "media", "content"),
    [
        (Kind.PDF, "application/pdf", b"<html></html>"),
        (Kind.DOCX, DOCX, pdf_bytes(1)),
        (Kind.SHEET, XLSX, b"plain text"),
        (Kind.SHEET, "application/vnd.ms-excel", zip_bytes()),
        (Kind.SHEET, "text/csv", b"a,b\x00c"),
        (Kind.IMAGE, "image/png", b"GIF89a"),
        (Kind.IMAGE, "image/svg+xml", b"<html>no svg</html>"),
        (Kind.TEXT, "text/plain", b"\x00\x01binary"),
        (Kind.MARKDOWN, "text/markdown", b"\xff\xfe\xfd not utf-8"),
    ],
)
def test_mismatched_content(kind: Kind, media: str, content: bytes) -> None:
    assert not content_matches(kind, media, content)
