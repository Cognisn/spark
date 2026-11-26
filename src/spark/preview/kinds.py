"""What a previewed document is: its renderer, and whether its content is what it says.

A document has a declared type (the response's ``Content-Type``, or the type stored
with a file) and a name. :func:`classify` turns them into one of the renderers the
preview frame has (:class:`Kind`), and :func:`content_matches` checks the declared
type against the first bytes of the content (magic bytes), so a response that says
it is a PDF and is in fact HTML is refused rather than shown.

A type the preview cannot show is :attr:`Kind.OTHER`: the pane shows its name, type
and size, with Download. A chart or diagram SPARK made (:data:`CHART_TYPE`,
:data:`DIAGRAM_TYPE`) is drawn from the chart API (``/charts/api/<file id>``), never
from its stored bytes.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = [
    "CHART_TYPE",
    "DIAGRAM_TYPE",
    "GENERIC_TYPES",
    "Kind",
    "base_type",
    "classify",
    "content_matches",
    "describe_type",
]


class Kind(enum.StrEnum):
    """The renderer a document is shown with."""

    PDF = "pdf"
    DOCX = "docx"
    SHEET = "sheet"
    IMAGE = "image"
    MARKDOWN = "markdown"
    TEXT = "text"
    # A chart or a Mermaid diagram SPARK keeps as an artefact: drawn from the chart API.
    CHART = "chart"
    DIAGRAM = "diagram"
    OTHER = "other"


# Types that say nothing about the content: the file name decides the renderer, and
# only when the content has that renderer's magic bytes.
GENERIC_TYPES: Final = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/x-download"}
)

_PDF: Final = frozenset({"application/pdf", "application/x-pdf"})
_DOCX: Final = frozenset(
    {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
)
_XLSX: Final = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    }
)
_XLS: Final = frozenset({"application/vnd.ms-excel", "application/msexcel"})
_CSV: Final = frozenset({"text/csv", "application/csv", "text/comma-separated-values"})
_IMAGES: Final = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}
)
_MARKDOWN: Final = frozenset({"text/markdown", "text/x-markdown"})
# The stored forms of create_chart and create_diagram (spark.charts.artefact).
CHART_TYPE: Final = "application/vnd.spark.chart+json"
DIAGRAM_TYPE: Final = "application/vnd.spark.diagram+json"
_TEXT_APPLICATION: Final = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
    }
)
# Text types the preview shows as text with line numbers, never as a page.
_NEVER_RENDERED: Final = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml"})

_BY_SUFFIX: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}

# Text files by name: shown as text with line numbers.
_TEXT_SUFFIXES: Final = frozenset(
    {
        ".txt",
        ".log",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".ts",
        ".py",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".sh",
        ".ps1",
        ".sql",
        ".tsv",
    }
)

_LABELS: Final[dict[Kind, str]] = {
    Kind.PDF: "PDF",
    Kind.DOCX: "Word document",
    Kind.SHEET: "spreadsheet",
    Kind.IMAGE: "image",
    Kind.MARKDOWN: "Markdown",
    Kind.TEXT: "text",
    Kind.CHART: "chart",
    Kind.DIAGRAM: "diagram",
    Kind.OTHER: "file",
}

_SNIFF_BYTES: Final = 8192


def base_type(content_type: str | None) -> str:
    """The media type without parameters, lower case (``text/plain; charset=x`` gives
    ``text/plain``)."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _suffix(name: str) -> str:
    name = name.rsplit("/", 1)[-1]
    return f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""


def _kind_of_type(media: str) -> Kind | None:
    if media == CHART_TYPE:
        return Kind.CHART
    if media == DIAGRAM_TYPE:
        return Kind.DIAGRAM
    if media in _PDF:
        return Kind.PDF
    if media in _DOCX:
        return Kind.DOCX
    if media in _XLSX or media in _XLS or media in _CSV:
        return Kind.SHEET
    if media in _IMAGES:
        return Kind.IMAGE
    if media in _MARKDOWN:
        return Kind.MARKDOWN
    if media.startswith("text/") or media in _TEXT_APPLICATION or media.endswith("+json"):
        return Kind.TEXT
    if media.endswith("+xml") and media not in _NEVER_RENDERED:
        return Kind.TEXT
    return None


def classify(content_type: str | None, name: str) -> tuple[Kind, str]:
    """The renderer for a document and the type it is treated as.

    The declared type decides; a generic declared type (:data:`GENERIC_TYPES`) takes
    the type its name implies. HTML is shown as text, never as a page.
    """
    media = base_type(content_type)
    if media in GENERIC_TYPES:
        suffix = _suffix(name)
        implied = _BY_SUFFIX.get(suffix)
        if implied is not None:
            media = implied
        elif suffix in _TEXT_SUFFIXES:
            return Kind.TEXT, "text/plain"
        else:
            return Kind.OTHER, media or "application/octet-stream"
    kind = _kind_of_type(media)
    if kind is None:
        return Kind.OTHER, media
    return kind, media


def _is_zip(head: bytes) -> bool:
    return head.startswith((b"PK\x03\x04", b"PK\x05\x06"))


def _is_cfb(head: bytes) -> bool:
    return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def _is_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    sample = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut at the end of the sample is still text.
        return exc.start >= len(sample) - 3
    return True


def _image_matches(media: str, head: bytes) -> bool:
    if media == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if media == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if media == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if media == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if media == "image/bmp":
        return head.startswith(b"BM")
    if media == "image/svg+xml":
        return _is_text(head) and b"<svg" in head.lower()
    return False


def content_matches(kind: Kind, media: str, head: bytes) -> bool:
    """Whether the content's first bytes are what ``media`` (classified as ``kind``) says.

    A document with no renderer (:attr:`Kind.OTHER`) is not shown, only offered for
    download, so any content is accepted for it.
    """
    head = head[:_SNIFF_BYTES]
    if kind is Kind.PDF:
        # PDF readers accept a header within the first kilobyte.
        return b"%PDF-" in head[:1024]
    if kind is Kind.DOCX:
        return _is_zip(head)
    if kind is Kind.SHEET:
        if media in _CSV:
            return _is_text(head)
        if media in _XLS:
            return _is_cfb(head)
        return _is_zip(head)
    if kind is Kind.IMAGE:
        return _image_matches(media, head)
    if kind in (Kind.MARKDOWN, Kind.TEXT):
        return _is_text(head)
    if kind in (Kind.CHART, Kind.DIAGRAM):
        return _is_text(head) and head.lstrip().startswith(b"{")
    return True


def describe_type(kind: Kind) -> str:
    """A plain label for the kind (``PDF``, ``Word document``)."""
    return _LABELS[kind]
