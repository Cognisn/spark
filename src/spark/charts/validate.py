"""Checking a chart definition, and shaping its data for the builder.

:func:`validate` checks a definition's structure against the vocabulary in
:mod:`spark.charts.schema`: the type, the fields, the encodings the type takes, its
options, the axis titles and the inline data (or the file it names). :func:`prepare`
then checks the data against the encodings (the columns exist, numbers are numbers,
labels are short plain text) and shapes it for the type, refusing anything past a
limit. Every refusal is a :class:`ChartRefused` whose message names what is wrong and
what is allowed, so the model can correct the definition and try again.

Text from a definition or its data is neutralised as it is shaped
(:func:`spark.files.titles.clean_text`): one line, no control characters and no ``<``
or ``>``. A label may not start with ``image://`` or ``path://`` (the forms ECharts
reads as pictures), so nothing in a chart can point anywhere.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias

from spark.charts.schema import (
    CHART_TYPES,
    DATA_FIELDS,
    ENCODINGS,
    FIELDS,
    LIMITS,
    OPTIONS,
    TYPES,
    TypeSpec,
)
from spark.files.titles import clean_text

__all__ = [
    "BoxShape",
    "CategoryShape",
    "Cell",
    "ChartRefused",
    "FileData",
    "FlowShape",
    "GridShape",
    "PointShape",
    "Prepared",
    "Shape",
    "SliceShape",
    "Spec",
    "Table",
    "TreeShape",
    "prepare",
    "validate",
]

Cell: TypeAlias = str | int | float | bool | None
_PICTURE_SCHEMES: Final = ("image://", "path://")


class ChartRefused(ValueError):
    """The definition cannot be drawn; the message says what to change."""


@dataclass(frozen=True)
class Table:
    """Columns and rows of cells."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]


@dataclass(frozen=True)
class FileData:
    """Data from a CSV or Excel file of the conversation, read when the chart is made."""

    file_id: int
    sheet: str | None = None
    columns: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Spec:
    """A checked definition. ``encoding`` maps each channel to its columns."""

    type: str
    encoding: Mapping[str, tuple[str, ...]]
    data: Table | FileData
    x_title: str = ""
    y_title: str = ""
    donut: bool = False
    stacked: bool = False
    horizontal: bool = False

    @property
    def type_spec(self) -> TypeSpec:
        return TYPES[self.type]

    def column(self, channel: str) -> str | None:
        found = self.encoding.get(channel, ())
        return found[0] if found else None

    def used_columns(self) -> tuple[str, ...]:
        seen: list[str] = []
        for channel in ENCODINGS:
            for name in self.encoding.get(channel, ()):
                if name not in seen:
                    seen.append(name)
        return tuple(seen)

    def definition(self, table: Table) -> dict[str, Any]:
        """The definition as stored: normalised, with ``table`` as its inline data."""
        out: dict[str, Any] = {
            "type": self.type,
            "data": {"columns": list(table.columns), "rows": [list(r) for r in table.rows]},
            "encoding": {k: (list(v) if len(v) > 1 else v[0]) for k, v in self.encoding.items()},
        }
        if self.x_title:
            out["x_title"] = self.x_title
        if self.y_title:
            out["y_title"] = self.y_title
        for option in OPTIONS:
            if getattr(self, option):
                out[option] = True
        return out


# -- Shapes: the data as each family of types draws it -------------------------------


@dataclass(frozen=True)
class CategoryShape:
    """bar, line, area and radar: categories, and one value per category per series."""

    categories: tuple[str, ...]
    series: tuple[tuple[str, tuple[float | None, ...]], ...]


@dataclass(frozen=True)
class PointShape:
    """scatter: points per series, each (x, y, size or None)."""

    series: tuple[tuple[str, tuple[tuple[float, float, float | None], ...]], ...]
    sized: bool


@dataclass(frozen=True)
class SliceShape:
    """pie and funnel: labelled values."""

    items: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class GridShape:
    """heatmap: two category axes and the value of each cell."""

    xs: tuple[str, ...]
    ys: tuple[str, ...]
    cells: tuple[tuple[int, int, float], ...]


@dataclass(frozen=True)
class TreeShape:
    """treemap: groups (None for no grouping) of labelled values."""

    groups: tuple[tuple[str | None, tuple[tuple[str, float], ...]], ...]


@dataclass(frozen=True)
class FlowShape:
    """sankey and graph: nodes, links (source, target, value) and node groups."""

    nodes: tuple[str, ...]
    links: tuple[tuple[str, str, float], ...]
    groups: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BoxShape:
    """boxplot: per group, [low whisker, Q1, median, Q3, high whisker] and outliers."""

    groups: tuple[tuple[str, tuple[float, float, float, float, float], tuple[float, ...]], ...]


Shape: TypeAlias = (
    CategoryShape | PointShape | SliceShape | GridShape | TreeShape | FlowShape | BoxShape
)


@dataclass(frozen=True)
class Prepared:
    """A checked definition with its data, shaped for its type."""

    spec: Spec
    table: Table
    shape: Shape


# -- Structure --------------------------------------------------------------------


def _names(values: Sequence[str]) -> str:
    return ", ".join(values)


def _text(value: Any, what: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ChartRefused(f"{what} must be text.")
    cleaned = clean_text(value, limit=limit + 1)
    if len(cleaned) > limit:
        raise ChartRefused(f"{what} is longer than {limit} characters; shorten it.")
    return cleaned


def _column_list(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ChartRefused(f"{what} must be a list of column names.")
    names = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ChartRefused(f"{what} must be a list of column names (text).")
        names.append(item)
    return tuple(names)


def _table(data: Mapping[str, Any]) -> Table:
    columns = _column_list(data.get("columns"), "data.columns")
    if len(columns) > LIMITS.columns:
        raise ChartRefused(
            f"data has {len(columns)} columns; a chart takes at most {LIMITS.columns}."
        )
    if len(set(columns)) != len(columns):
        raise ChartRefused("data.columns has a name twice; each column needs its own name.")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ChartRefused("data.rows must be a list of rows, each a list of cells.")
    out: list[tuple[Cell, ...]] = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, list) or len(row) != len(columns):
            raise ChartRefused(
                f"Row {number} must be a list of {len(columns)} cells, one per column."
            )
        for cell in row:
            if cell is not None and not isinstance(cell, (str, int, float)):
                raise ChartRefused(f"Row {number} has a cell that is not text, a number or empty.")
        out.append(tuple(row))
    return Table(columns, tuple(out))


def _data(value: Any) -> Table | FileData:
    if not isinstance(value, dict):
        raise ChartRefused(
            "data must be an object: inline {columns, rows}, or a file of this conversation "
            "{file_id, sheet, columns}."
        )
    unknown = sorted(set(value) - set(DATA_FIELDS))
    if unknown:
        raise ChartRefused(
            f"data has unknown fields: {_names(unknown)}. Allowed: {_names(DATA_FIELDS)}."
        )
    if "file_id" in value:
        if "rows" in value:
            raise ChartRefused("data takes either rows (inline) or file_id (a file), not both.")
        file_id = value["file_id"]
        if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id <= 0:
            raise ChartRefused("data.file_id must be a file id, such as one from list_my_files.")
        sheet = value.get("sheet")
        if sheet is not None and not isinstance(sheet, str):
            raise ChartRefused("data.sheet must be the name of a sheet.")
        columns = value.get("columns")
        chosen = _column_list(columns, "data.columns") if columns is not None else None
        return FileData(file_id, sheet, chosen)
    if "sheet" in value:
        raise ChartRefused("data.sheet applies to a file (file_id) only.")
    return _table(value)


def _encoding(spec: TypeSpec, value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or not value:
        raise ChartRefused(
            f"encoding must say which column plays each part; {spec.name} takes "
            f"{_names([c.name for c in spec.channels])}."
        )
    unknown = sorted(set(value) - set(ENCODINGS))
    if unknown:
        raise ChartRefused(
            f"encoding has unknown parts: {_names(unknown)}. Allowed: {_names(ENCODINGS)}."
        )
    out: dict[str, tuple[str, ...]] = {}
    for name, columns in value.items():
        channel = spec.channel(name)
        if channel is None:
            raise ChartRefused(
                f"A {spec.name} chart does not use {name}; it takes "
                f"{_names([c.name for c in spec.channels])}."
            )
        if channel.many and isinstance(columns, list):
            out[name] = _column_list(columns, f"encoding.{name}")
        elif isinstance(columns, str) and columns.strip():
            out[name] = (columns,)
        else:
            many = " or a list of column names" if channel.many else ""
            raise ChartRefused(f"encoding.{name} must be a column name{many}.")
    missing = [c.name for c in spec.channels if c.required and c.name not in out]
    if missing:
        raise ChartRefused(
            f"A {spec.name} chart needs encoding.{_names(missing)} "
            f"({'; '.join(f'{c.name}: {c.meaning}' for c in spec.channels if c.name in missing)})."
        )
    many_y = len(out.get("y", ())) > 1
    if many_y and "series" in out:
        raise ChartRefused(
            "Give either several y columns (one series each) or a series column, not both."
        )
    if many_y and len(out["y"]) > LIMITS.series:
        raise ChartRefused(f"encoding.y names {len(out['y'])} columns; at most {LIMITS.series}.")
    return out


def validate(definition: Any) -> Spec:
    """Check a definition's structure and vocabulary; raise :class:`ChartRefused`."""
    if not isinstance(definition, dict):
        raise ChartRefused("definition must be an object with type, data and encoding.")
    unknown = sorted(set(definition) - set(FIELDS))
    if unknown:
        raise ChartRefused(
            f"definition has unknown fields: {_names(unknown)}. Allowed: {_names(FIELDS)}."
        )
    kind = definition.get("type")
    if kind not in CHART_TYPES:
        raise ChartRefused(f"type must be one of: {_names(CHART_TYPES)}.")
    spec = TYPES[str(kind)]
    options: dict[str, bool] = {}
    for option in OPTIONS:
        if option not in definition:
            continue
        if option not in spec.options:
            allowed = [n for n, t in TYPES.items() if option in t.options]
            raise ChartRefused(f"{option} applies to {_names(allowed)} charts only.")
        if not isinstance(definition[option], bool):
            raise ChartRefused(f"{option} must be true or false.")
        options[option] = definition[option]
    titles: dict[str, str] = {}
    for name in ("x_title", "y_title"):
        if definition.get(name) is None:
            continue
        if not spec.axes:
            allowed = [n for n, t in TYPES.items() if t.axes]
            raise ChartRefused(f"{name} applies to {_names(allowed)} charts only.")
        titles[name] = _text(definition[name], name, LIMITS.axis_title)
    if "data" not in definition:
        raise ChartRefused("definition needs data: inline {columns, rows} or {file_id}.")
    data = _data(definition["data"])
    encoding = _encoding(spec, definition.get("encoding"))
    return Spec(
        spec.name,
        encoding,
        data,
        x_title=titles.get("x_title", ""),
        y_title=titles.get("y_title", ""),
        donut=options.get("donut", False),
        stacked=options.get("stacked", False),
        horizontal=options.get("horizontal", False),
    )


# -- Data -----------------------------------------------------------------------


def _label(value: Cell, column: str) -> str:
    if value is None or value == "":
        return "(blank)"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = clean_text(value, limit=LIMITS.label + 1)
    if len(text) > LIMITS.label:
        raise ChartRefused(
            f"A label in column {clean_text(column, limit=LIMITS.label)} is longer than "
            f"{LIMITS.label} characters; shorten the labels."
        )
    if text.lower().startswith(_PICTURE_SCHEMES):
        raise ChartRefused("A label may not start with image:// or path://.")
    return text


def _number(value: Cell, column: str, row: int) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ChartRefused(f"Column {column} must hold numbers; row {row} has true or false.")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip().replace(",", ""))
        except ValueError:
            shown = clean_text(value, limit=40)
            raise ChartRefused(
                f"Column {column} must hold numbers; row {row} has '{shown}'."
            ) from None
    if not math.isfinite(number):
        raise ChartRefused(f"Column {column} must hold finite numbers; row {row} does not.")
    return number


def _restrict(spec: Spec, table: Table) -> Table:
    """The table with only the columns the encodings use, after checking they exist."""
    missing = [c for c in spec.used_columns() if c not in table.columns]
    if missing:
        shown = _names([clean_text(c, limit=LIMITS.label) for c in table.columns])
        raise ChartRefused(
            f"The data has no column {_names([clean_text(m, limit=LIMITS.label) for m in missing])}. "
            f"Its columns are: {shown}."
        )
    keep = [table.columns.index(c) for c in spec.used_columns()]
    return Table(
        tuple(table.columns[i] for i in keep), tuple(tuple(r[i] for i in keep) for r in table.rows)
    )


Getter: TypeAlias = Callable[[tuple[Cell, ...]], Cell]


def _getter(table: Table, column: str | None) -> Getter | None:
    if column is None:
        return None
    index = table.columns.index(column)
    return lambda row: row[index]


def _ordered(values: Sequence[str], limit: int, what: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    if len(seen) > limit:
        raise ChartRefused(
            f"The data has {len(seen)} {what}; this chart takes at most {limit}. "
            "Group the smaller ones or filter the data first."
        )
    return tuple(seen)


def _categorical(spec: Spec, table: Table, limit: int, what: str) -> CategoryShape:
    x = _getter(table, spec.column("x"))
    assert x is not None
    x_name = spec.column("x") or ""
    categories = _ordered([_label(x(r), x_name) for r in table.rows], limit, what)
    sums: dict[str, dict[str, float | None]] = {}
    y_columns = spec.encoding["y"]
    series_column = spec.column("series")
    names: list[str]
    if series_column is not None:
        split = _getter(table, series_column)
        assert split is not None
        names = list(
            _ordered([_label(split(r), series_column) for r in table.rows], LIMITS.series, "series")
        )
    else:
        names = [_label(c, c) for c in y_columns]
    for index, row in enumerate(table.rows, 1):
        category = _label(x(row), x_name)
        pairs: list[tuple[str, str]]
        if series_column is not None:
            split = _getter(table, series_column)
            assert split is not None
            pairs = [(_label(split(row), series_column), y_columns[0])]
        else:
            pairs = [(_label(c, c), c) for c in y_columns]
        for name, column in pairs:
            value = _number(row[table.columns.index(column)], column, index)
            slot = sums.setdefault(name, {})
            if value is None:
                slot.setdefault(category, None)
            else:
                slot[category] = (slot.get(category) or 0.0) + value
    return CategoryShape(
        categories,
        tuple((name, tuple(sums.get(name, {}).get(c) for c in categories)) for name in names),
    )


def _points(spec: Spec, table: Table) -> PointShape:
    xs, ys = spec.column("x") or "", spec.column("y") or ""
    size = spec.column("size")
    group = spec.column("series")
    names: tuple[str, ...] = ("",)
    if group is not None:
        names = _ordered(
            [_label(r[table.columns.index(group)], group) for r in table.rows],
            LIMITS.series,
            "series",
        )
    points: dict[str, list[tuple[float, float, float | None]]] = {n: [] for n in names}
    for index, row in enumerate(table.rows, 1):
        x = _number(row[table.columns.index(xs)], xs, index)
        y = _number(row[table.columns.index(ys)], ys, index)
        s = _number(row[table.columns.index(size)], size, index) if size else None
        if x is None or y is None or (size is not None and s is None):
            continue
        if s is not None and s < 0:
            raise ChartRefused(f"Column {size} sizes points, so it must be zero or more.")
        name = _label(row[table.columns.index(group)], group) if group else ""
        points[name].append((x, y, s))
    if not any(points.values()):
        raise ChartRefused("No row has numbers in both x and y.")
    label = clean_text(ys, limit=LIMITS.label)
    return PointShape(
        tuple((n or label, tuple(p)) for n, p in points.items()), sized=size is not None
    )


def _labelled(
    spec: Spec, table: Table, limit: int, what: str, positive: bool
) -> tuple[tuple[str, float], ...]:
    x, y = spec.column("x") or "", spec.column("y") or ""
    sums: dict[str, float] = {}
    for index, row in enumerate(table.rows, 1):
        value = _number(row[table.columns.index(y)], y, index)
        if value is None:
            continue
        if value < 0 or (positive and value == 0):
            need = "more than zero" if positive else "zero or more"
            raise ChartRefused(
                f"A {spec.type} chart needs values {need}; column {y} has {value:g}."
            )
        label = _label(row[table.columns.index(x)], x)
        sums[label] = sums.get(label, 0.0) + value
    if len(sums) > limit:
        raise ChartRefused(
            f"The data has {len(sums)} {what}; a {spec.type} chart takes at most {limit}. "
            "Group the smaller ones (for example as Other) first."
        )
    if not sums:
        raise ChartRefused(f"No row has a number in {y}.")
    return tuple(sums.items())


def _grid(spec: Spec, table: Table) -> GridShape:
    xn, yn, vn = spec.column("x") or "", spec.column("y") or "", spec.column("colour") or ""
    xs = _ordered(
        [_label(r[table.columns.index(xn)], xn) for r in table.rows], LIMITS.categories, "columns"
    )
    ys = _ordered(
        [_label(r[table.columns.index(yn)], yn) for r in table.rows], LIMITS.categories, "rows"
    )
    sums: dict[tuple[int, int], float] = {}
    for index, row in enumerate(table.rows, 1):
        value = _number(row[table.columns.index(vn)], vn, index)
        if value is None:
            continue
        key = (
            xs.index(_label(row[table.columns.index(xn)], xn)),
            ys.index(_label(row[table.columns.index(yn)], yn)),
        )
        sums[key] = sums.get(key, 0.0) + value
    if not sums:
        raise ChartRefused(f"No row has a number in {vn}.")
    return GridShape(xs, ys, tuple((x, y, v) for (x, y), v in sums.items()))


def _tree(spec: Spec, table: Table) -> TreeShape:
    group = spec.column("series")
    if group is None:
        return TreeShape(((None, _labelled(spec, table, LIMITS.leaves, "rectangles", True)),))
    names = _ordered(
        [_label(r[table.columns.index(group)], group) for r in table.rows], LIMITS.series, "groups"
    )
    groups = []
    total = 0
    for name in names:
        rows = tuple(r for r in table.rows if _label(r[table.columns.index(group)], group) == name)
        try:
            items = _labelled(spec, Table(table.columns, rows), LIMITS.leaves, "rectangles", True)
        except ChartRefused as exc:
            if str(exc).startswith("No row has"):
                continue
            raise
        total += len(items)
        groups.append((name, items))
    if total > LIMITS.leaves:
        raise ChartRefused(
            f"The data has {total} rectangles; a treemap takes at most {LIMITS.leaves}."
        )
    if not groups:
        raise ChartRefused(f"No row has a number in {spec.column('y')}.")
    return TreeShape(tuple(groups))


def _has_cycle(nodes: Sequence[str], links: Sequence[tuple[str, str, float]]) -> bool:
    following: dict[str, list[str]] = {n: [] for n in nodes}
    for source, target, _ in links:
        following[source].append(target)
    state: dict[str, int] = {}
    for start in nodes:
        if state.get(start):
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        state[start] = 1
        while stack:
            node, at = stack[-1]
            if at < len(following[node]):
                stack[-1] = (node, at + 1)
                nxt = following[node][at]
                mark = state.get(nxt, 0)
                if mark == 1:
                    return True
                if mark == 0:
                    state[nxt] = 1
                    stack.append((nxt, 0))
            else:
                state[node] = 2
                stack.pop()
    return False


def _flow(spec: Spec, table: Table) -> FlowShape:
    sn, tn = spec.column("x") or "", spec.column("y") or ""
    size, group = spec.column("size"), spec.column("series")
    weights: dict[tuple[str, str], float] = {}
    groups: dict[str, str] = {}
    nodes: dict[str, None] = {}
    for index, row in enumerate(table.rows, 1):
        source = _label(row[table.columns.index(sn)], sn)
        target = _label(row[table.columns.index(tn)], tn)
        value = _number(row[table.columns.index(size)], size, index) if size else 1.0
        if value is None:
            continue
        if value <= 0:
            raise ChartRefused(
                f"Column {size} carries link values, so each must be more than zero."
            )
        if source == target:
            raise ChartRefused(f"Row {index} links {source} to itself; a link needs two nodes.")
        nodes.setdefault(source, None)
        nodes.setdefault(target, None)
        if group is not None:
            groups.setdefault(source, _label(row[table.columns.index(group)], group))
        weights[(source, target)] = weights.get((source, target), 0.0) + value
    if len(nodes) > LIMITS.nodes:
        raise ChartRefused(
            f"The data has {len(nodes)} nodes; a {spec.type} chart takes at most {LIMITS.nodes}. "
            "Keep the largest and group the rest first."
        )
    if len(weights) > LIMITS.links:
        raise ChartRefused(
            f"The data has {len(weights)} links; a {spec.type} chart takes at most {LIMITS.links}."
        )
    if not weights:
        raise ChartRefused("No row has a link with a value.")
    links = tuple((s, t, v) for (s, t), v in weights.items())
    if spec.type == "sankey" and _has_cycle(tuple(nodes), links):
        raise ChartRefused(
            "A sankey chart cannot have a cycle (flows that lead back to where they started); "
            "use a graph chart for a network instead."
        )
    if group is not None:
        distinct = set(groups.values())
        if len(distinct) > LIMITS.series:
            raise ChartRefused(f"The data has {len(distinct)} groups; at most {LIMITS.series}.")
    return FlowShape(tuple(nodes), links, groups)


def _quantile(ordered: Sequence[float], q: float) -> float:
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _boxes(spec: Spec, table: Table) -> BoxShape:
    xn, yn = spec.column("x") or "", spec.column("y") or ""
    names = _ordered(
        [_label(r[table.columns.index(xn)], xn) for r in table.rows], LIMITS.categories, "groups"
    )
    values: dict[str, list[float]] = {n: [] for n in names}
    for index, row in enumerate(table.rows, 1):
        value = _number(row[table.columns.index(yn)], yn, index)
        if value is not None:
            values[_label(row[table.columns.index(xn)], xn)].append(value)
    out = []
    for name in names:
        ordered = sorted(values[name])
        if not ordered:
            continue
        q1, median, q3 = (_quantile(ordered, q) for q in (0.25, 0.5, 0.75))
        reach = 1.5 * (q3 - q1)
        inside = [v for v in ordered if q1 - reach <= v <= q3 + reach]
        outliers = tuple(v for v in ordered if v < q1 - reach or v > q3 + reach)
        out.append((name, (inside[0], q1, median, q3, inside[-1]), outliers))
    if not out:
        raise ChartRefused(f"No row has a number in {yn}.")
    return BoxShape(tuple(out))


def _shape(spec: Spec, table: Table) -> Shape:
    kind = spec.type
    if kind in ("bar", "line", "area"):
        return _categorical(spec, table, LIMITS.categories, "categories")
    if kind == "radar":
        shape = _categorical(spec, table, LIMITS.indicators_max, "measures")
        if len(shape.categories) < LIMITS.indicators_min:
            raise ChartRefused(
                f"A radar chart needs at least {LIMITS.indicators_min} measures in x; "
                f"the data has {len(shape.categories)}."
            )
        for _, values in shape.series:
            if any(v is not None and v < 0 for v in values):
                raise ChartRefused("A radar chart needs values of zero or more.")
        return shape
    if kind == "scatter":
        return _points(spec, table)
    if kind in ("pie", "funnel"):
        return SliceShape(_labelled(spec, table, LIMITS.slices, "slices", False))
    if kind == "heatmap":
        return _grid(spec, table)
    if kind == "treemap":
        return _tree(spec, table)
    if kind in ("sankey", "graph"):
        return _flow(spec, table)
    return _boxes(spec, table)


def prepare(spec: Spec, table: Table) -> Prepared:
    """Check ``table`` against the definition and shape it; raise :class:`ChartRefused`.

    ``table`` is the definition's inline data, or the file it names as read. Only the
    columns the encodings use are kept (the snapshot stored with the chart).
    """
    if len(table.rows) > LIMITS.rows:
        raise ChartRefused(
            f"The data has {len(table.rows):,} rows; a chart takes at most {LIMITS.rows:,}. "
            "Summarise or filter the data first."
        )
    used = _restrict(spec, table)
    return Prepared(spec, used, _shape(spec, used))
