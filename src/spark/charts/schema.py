"""The SPARK chart definition: a closed vocabulary the model fills in (PC19).

The model never writes ECharts JSON or JavaScript. It names a chart type from
:data:`CHART_TYPES`, gives its data (inline columns and rows, or a CSV or Excel file
in the conversation by file id), says which columns play which part (the encodings
in :data:`ENCODINGS`), and may add axis titles and the few options its type takes.
SPARK checks every field (:mod:`spark.charts.validate`) and builds the ECharts option
itself (:mod:`spark.charts.build`).

This module is the reference for everything a definition may hold: the chart skill
documents each type, field, encoding and limit here, and a coverage test reads
:func:`describe` to check that it does.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

__all__ = [
    "CHART_TYPES",
    "DATA_FIELDS",
    "ENCODINGS",
    "FIELDS",
    "LIMITS",
    "OPTIONS",
    "TYPES",
    "Channel",
    "Limits",
    "TypeSpec",
    "describe",
]

CHART_TYPES: Final = (
    "bar",
    "line",
    "area",
    "scatter",
    "pie",
    "heatmap",
    "treemap",
    "sankey",
    "graph",
    "radar",
    "funnel",
    "boxplot",
)
ENCODINGS: Final = ("x", "y", "series", "size", "colour")
# Options a type may take: a pie drawn as a ring, stacked series, horizontal bars.
OPTIONS: Final = ("donut", "stacked", "horizontal")
FIELDS: Final = ("type", "data", "encoding", "x_title", "y_title", *OPTIONS)
# data: inline {columns, rows}, or a file of the conversation {file_id, sheet, columns}.
DATA_FIELDS: Final = ("columns", "rows", "file_id", "sheet")


@dataclass(frozen=True)
class Limits:
    """How much one chart may hold. Past a limit the definition is refused with a
    message saying what to change (group, filter or summarise first)."""

    rows: int = 5000
    columns: int = 50
    series: int = 8
    categories: int = 100
    slices: int = 8
    leaves: int = 200
    nodes: int = 200
    links: int = 1000
    indicators_min: int = 3
    indicators_max: int = 20
    label: int = 80
    axis_title: int = 80
    caption: int = 500
    # Network and Sankey node labels show on hover only above this many nodes.
    labels_on_hover_above: int = 40


LIMITS: Final = Limits()


@dataclass(frozen=True)
class Channel:
    """One encoding of a chart type: which column plays which part."""

    name: str
    meaning: str
    required: bool
    numeric: bool = False
    many: bool = False  # several columns may be named (one series each)


@dataclass(frozen=True)
class TypeSpec:
    """A chart type: its encodings, the options it takes and whether it has axes."""

    name: str
    summary: str
    channels: tuple[Channel, ...]
    options: tuple[str, ...] = ()
    axes: bool = False

    def channel(self, name: str) -> Channel | None:
        return next((c for c in self.channels if c.name == name), None)


def _categorical(name: str, summary: str, options: tuple[str, ...]) -> TypeSpec:
    return TypeSpec(
        name,
        summary,
        (
            Channel("x", "category on the horizontal axis", True),
            Channel("y", "value (one column per series, or one with series)", True, True, True),
            Channel("series", "column whose values split the data into series", False),
        ),
        options,
        axes=True,
    )


TYPES: Final[dict[str, TypeSpec]] = {
    "bar": _categorical(
        "bar", "Bars per category; stacked or horizontal.", ("stacked", "horizontal")
    ),
    "line": _categorical("line", "Lines across ordered categories such as dates.", ("stacked",)),
    "area": _categorical("area", "Filled lines; stacked shows parts of a total.", ("stacked",)),
    "scatter": TypeSpec(
        "scatter",
        "Points by two numbers; size can carry a third.",
        (
            Channel("x", "number on the horizontal axis", True, True),
            Channel("y", "number on the vertical axis", True, True),
            Channel("series", "column whose values group the points", False),
            Channel("size", "number that sizes each point", False, True),
        ),
        axes=True,
    ),
    "pie": TypeSpec(
        "pie",
        "Parts of a whole; donut draws a ring.",
        (Channel("x", "slice label", True), Channel("y", "slice value (zero or more)", True, True)),
        ("donut",),
    ),
    "heatmap": TypeSpec(
        "heatmap",
        "A grid of two categories coloured by a value.",
        (
            Channel("x", "column category", True),
            Channel("y", "row category", True),
            Channel("colour", "value that colours each cell", True, True),
        ),
        axes=True,
    ),
    "treemap": TypeSpec(
        "treemap",
        "Nested rectangles sized by value; series groups them.",
        (
            Channel("x", "rectangle label", True),
            Channel("y", "rectangle size (more than zero)", True, True),
            Channel("series", "parent group of each rectangle", False),
        ),
    ),
    "sankey": TypeSpec(
        "sankey",
        "Flows from sources to targets; no cycles.",
        (
            Channel("x", "source node", True),
            Channel("y", "target node", True),
            Channel("size", "flow value (more than zero)", True, True),
        ),
    ),
    "graph": TypeSpec(
        "graph",
        "A network of nodes and links.",
        (
            Channel("x", "source node", True),
            Channel("y", "target node", True),
            Channel("size", "link weight (more than zero; default 1 per row)", False, True),
            Channel("series", "group of the source node", False),
        ),
    ),
    "radar": TypeSpec(
        "radar",
        "Several measures on spokes, one shape per series.",
        (
            Channel("x", "measure (one spoke each)", True),
            Channel("y", "value (zero or more; one column per series)", True, True, True),
            Channel("series", "column whose values make the shapes", False),
        ),
    ),
    "funnel": TypeSpec(
        "funnel",
        "Stages that narrow, largest first.",
        (Channel("x", "stage", True), Channel("y", "value (zero or more)", True, True)),
    ),
    "boxplot": TypeSpec(
        "boxplot",
        "The spread of values per group: quartiles, whiskers and outliers.",
        (Channel("x", "group", True), Channel("y", "raw values to summarise", True, True)),
        axes=True,
    ),
}


def describe() -> dict[str, Any]:
    """The whole vocabulary as data: types with their encodings, fields and limits."""
    return {
        "types": {
            name: {
                "summary": spec.summary,
                "encodings": {c.name: asdict(c) for c in spec.channels},
                "options": list(spec.options),
                "axis_titles": spec.axes,
            }
            for name, spec in TYPES.items()
        },
        "fields": list(FIELDS),
        "data_fields": list(DATA_FIELDS),
        "encodings": list(ENCODINGS),
        "options": list(OPTIONS),
        "limits": asdict(LIMITS),
    }
