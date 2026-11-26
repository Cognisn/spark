"""Charts and diagrams as artefacts: what is stored, and what the page is given.

A chart is stored as one JSON document (:data:`CHART_FORMAT`): its title and caption,
its definition with the data snapshot inline (only the columns it uses), and where the
data came from. A diagram (:data:`DIAGRAM_FORMAT`) holds its title, caption and
Mermaid source. Both are files in the creator's area (kinds ``chart`` and
``diagram``), charged to their quota like any other.

Reading one back checks it again exactly as a new definition is checked, so a stored
file that was changed or damaged is refused rather than drawn. :func:`payload` is
what ``GET /charts/api/<file id>`` answers: the option SPARK built for the asked
theme (never anything the model wrote as code), or the diagram's source.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Any, Final

from spark.charts.build import build_option
from spark.charts.schema import LIMITS
from spark.charts.theme import Theme
from spark.charts.validate import ChartRefused, FileData, Prepared, Table, prepare, validate
from spark.files.titles import TITLE_LIMIT, clean_text, require_title
from spark.preview.kinds import CHART_TYPE, DIAGRAM_TYPE

__all__ = [
    "CHART_FORMAT",
    "CHART_MIME",
    "DIAGRAM_FORMAT",
    "DIAGRAM_LIMIT",
    "DIAGRAM_MIME",
    "Chart",
    "Diagram",
    "caption_text",
    "chart_document",
    "csv_bytes",
    "diagram_document",
    "diagram_source",
    "new_chart",
    "payload",
    "read_artefact",
]

CHART_FORMAT: Final = "spark-chart/1"
DIAGRAM_FORMAT: Final = "spark-diagram/1"
CHART_MIME: Final = CHART_TYPE
DIAGRAM_MIME: Final = DIAGRAM_TYPE
DIAGRAM_LIMIT: Final = 20_000
# A spreadsheet runs a cell starting with these as a formula; saved data never does.
_FORMULA: Final = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Chart:
    title: str
    caption: str
    prepared: Prepared
    source: dict[str, Any] | None = None


@dataclass(frozen=True)
class Diagram:
    title: str
    caption: str
    source: str


def caption_text(value: Any) -> str:
    """A caption as plain text, refusing one that is too long."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ChartRefused("caption must be text.")
    cleaned = clean_text(value, limit=LIMITS.caption + 1)
    if len(cleaned) > LIMITS.caption:
        raise ChartRefused(f"caption is longer than {LIMITS.caption} characters; shorten it.")
    return cleaned


def diagram_source(value: Any) -> str:
    """Mermaid source to store: text, not empty, within the limit, settings untouched.

    A diagram may not carry Mermaid directives or a configuration block, which could
    change how it is drawn (for example turn HTML labels back on).
    """
    if not isinstance(value, str) or not value.strip():
        raise ChartRefused("source must be the Mermaid text of the diagram.")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > DIAGRAM_LIMIT:
        raise ChartRefused(f"source is longer than {DIAGRAM_LIMIT:,} characters; simplify it.")
    if "\x00" in text:
        raise ChartRefused("source must be plain text.")
    lowered = text.lower()
    if "%%{" in text or (lowered.startswith("---") and "config" in lowered.split("---", 2)[1]):
        raise ChartRefused(
            "A diagram may not change Mermaid's settings (no %%{init}%% directive or config "
            "block); write only the diagram."
        )
    return text


def chart_document(chart: Chart) -> bytes:
    """The stored JSON of a chart."""
    spec, table = chart.prepared.spec, chart.prepared.table
    body = {
        "format": CHART_FORMAT,
        "title": chart.title,
        "caption": chart.caption,
        "definition": spec.definition(table),
        "source": chart.source,
    }
    return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def diagram_document(diagram: Diagram) -> bytes:
    """The stored JSON of a diagram."""
    body = {
        "format": DIAGRAM_FORMAT,
        "title": diagram.title,
        "caption": diagram.caption,
        "source": diagram.source,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def read_artefact(raw: bytes) -> Chart | Diagram:
    """A stored chart or diagram, checked again; raises :class:`ChartRefused`."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChartRefused("This file is not a chart or a diagram.") from exc
    if not isinstance(body, dict):
        raise ChartRefused("This file is not a chart or a diagram.")
    title = clean_text(body.get("title"), limit=TITLE_LIMIT) or "Untitled"
    caption = caption_text(body.get("caption"))
    if body.get("format") == DIAGRAM_FORMAT:
        return Diagram(title, caption, diagram_source(body.get("source")))
    if body.get("format") != CHART_FORMAT:
        raise ChartRefused("This file is not a chart or a diagram.")
    spec = validate(body.get("definition"))
    if isinstance(spec.data, FileData):
        raise ChartRefused("A stored chart holds its data.")
    source = body.get("source")
    return Chart(
        title, caption, prepare(spec, spec.data), source if isinstance(source, dict) else None
    )


def new_chart(title: Any, caption: Any, prepared: Prepared, data: FileData | None) -> Chart:
    """A chart to store, with its title and caption neutralised as text."""
    source = None
    if data is not None:
        source = {"file_id": data.file_id, "sheet": data.sheet}
    return Chart(require_title(title), caption_text(caption), prepared, source)


def payload(artefact: Chart | Diagram, theme: Theme, *, export: bool = False) -> dict[str, Any]:
    """What the page draws: the option built for ``theme``, or the diagram's source.

    Each carries the title, the caption and the theme's background and text colours
    (for a saved image's frame).
    """
    common = {
        "title": artefact.title,
        "caption": artefact.caption,
        "theme": theme.name,
        "background": theme.background,
        "text": theme.text,
    }
    if isinstance(artefact, Diagram):
        return {"kind": "diagram", **common, "source": artefact.source}
    option = build_option(
        artefact.prepared, theme, title=artefact.title, caption=artefact.caption, export=export
    )
    return {
        "kind": "chart",
        **common,
        "type": artefact.prepared.spec.type,
        "rows": len(artefact.prepared.table.rows),
        "option": option,
    }


def _safe_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA):
        return f"'{value}"
    return str(value)


def csv_bytes(chart: Chart) -> bytes:
    """The chart's data snapshot as CSV; no cell can run as a spreadsheet formula."""
    table: Table = chart.prepared.table
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow([_safe_cell(c) for c in table.columns])
    for row in table.rows:
        writer.writerow([_safe_cell(c) for c in row])
    return out.getvalue().encode("utf-8-sig")
