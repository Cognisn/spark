"""The chart definition: every refusal rule, the option per type, and that it is data only.

* Each rule of :mod:`spark.charts.validate` refuses with a message that names what is
  allowed (one test per rule, each mutation-checked).
* :func:`spark.charts.build.build_option` gives each of the twelve types the option
  shape recorded in ``tests/chart_option_shapes.json`` (keys and value types; run
  with ``SPARK_UPDATE_CHART_SHAPES=1`` to record a deliberate change).
* The option is data only: whatever text a definition carries, in any field, no
  function, markup, picture, link or model-written formatter reaches the option, and
  every colour is the theme's.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark.charts import schema
from spark.charts.artefact import (
    Chart,
    Diagram,
    caption_text,
    chart_document,
    csv_bytes,
    diagram_document,
    diagram_source,
    payload,
    read_artefact,
)
from spark.charts.build import FORMATTERS, build_option
from spark.charts.theme import DARK, LIGHT, THEMES
from spark.charts.validate import ChartRefused, FileData, Prepared, prepare, validate

SHAPES = Path(__file__).with_name("chart_option_shapes.json")


def table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows}


SALES = table(
    ["Quarter", "North", "South", "Region", "Sales"],
    [
        ["Q1", 10, 12, "North", 10],
        ["Q2", 14, 9, "South", 9],
        ["Q3", 18, 15, "North", 18],
        ["Q4", 12, 20, "South", 20],
    ],
)
FLOWS = table(
    ["From", "To", "Amount", "Team"],
    [
        ["Email", "Visit", 120, "Marketing"],
        ["Search", "Visit", 300, "Marketing"],
        ["Visit", "Sign up", 90, "Product"],
        ["Visit", "Leave", 330, "Product"],
        ["Sign up", "Buy", 40, "Sales"],
    ],
)
GOOD: dict[str, dict[str, Any]] = {
    "bar": {
        "type": "bar",
        "data": SALES,
        "encoding": {"x": "Quarter", "y": ["North", "South"]},
        "x_title": "Quarter",
        "y_title": "Units",
        "stacked": True,
    },
    "line": {
        "type": "line",
        "data": SALES,
        "encoding": {"x": "Quarter", "y": "Sales", "series": "Region"},
    },
    "area": {
        "type": "area",
        "data": SALES,
        "encoding": {"x": "Quarter", "y": ["North", "South"]},
        "stacked": True,
    },
    "scatter": {
        "type": "scatter",
        "data": table(["a", "b", "c", "g"], [[1, 2, 3, "x"], [2, 4, 1, "y"], [3, 1, 9, "x"]]),
        "encoding": {"x": "a", "y": "b", "size": "c", "series": "g"},
    },
    "pie": {"type": "pie", "data": SALES, "encoding": {"x": "Region", "y": "Sales"}, "donut": True},
    "heatmap": {
        "type": "heatmap",
        "data": table(
            ["Day", "Hour", "Calls"],
            [["Mon", "9", 4], ["Mon", "10", 7], ["Tue", "9", 2], ["Tue", "10", 9]],
        ),
        "encoding": {"x": "Hour", "y": "Day", "colour": "Calls"},
    },
    "treemap": {
        "type": "treemap",
        "data": table(["Item", "Size", "Group"], [["a", 4, "G1"], ["b", 2, "G1"], ["c", 5, "G2"]]),
        "encoding": {"x": "Item", "y": "Size", "series": "Group"},
    },
    "sankey": {
        "type": "sankey",
        "data": FLOWS,
        "encoding": {"x": "From", "y": "To", "size": "Amount"},
    },
    "graph": {
        "type": "graph",
        "data": FLOWS,
        "encoding": {"x": "From", "y": "To", "size": "Amount", "series": "Team"},
    },
    "radar": {
        "type": "radar",
        "data": table(["Skill", "Ada", "Bo"], [["Speed", 3, 4], ["Care", 5, 2], ["Reach", 4, 4]]),
        "encoding": {"x": "Skill", "y": ["Ada", "Bo"]},
    },
    "funnel": {
        "type": "funnel",
        "data": table(["Stage", "People"], [["Visit", 400], ["Sign up", 90], ["Buy", 40]]),
        "encoding": {"x": "Stage", "y": "People"},
    },
    "boxplot": {
        "type": "boxplot",
        "data": table(
            ["Team", "Days"],
            [["A", 1], ["A", 2], ["A", 3], ["A", 4], ["A", 40], ["B", 5], ["B", 6], ["B", 8]],
        ),
        "encoding": {"x": "Team", "y": "Days"},
        "y_title": "Days",
    },
}


def make(name: str) -> Prepared:
    spec = validate(GOOD[name])
    assert not isinstance(spec.data, FileData)
    return prepare(spec, spec.data)


def refused(definition: Any) -> str:
    with pytest.raises(ChartRefused) as info:
        spec = validate(definition)
        assert not isinstance(spec.data, FileData)
        prepare(spec, spec.data)
    return str(info.value)


def with_(name: str, **changes: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(GOOD[name]))
    out.update(changes)
    return out


def test_the_vocabulary_is_the_rulings() -> None:
    assert schema.CHART_TYPES == (
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
    assert schema.ENCODINGS == ("x", "y", "series", "size", "colour")
    assert set(GOOD) == set(schema.CHART_TYPES) == set(schema.TYPES)
    described = schema.describe()
    assert set(described["types"]) == set(schema.CHART_TYPES)
    assert described["limits"]["rows"] == schema.LIMITS.rows
    json.dumps(described)


@pytest.mark.parametrize("name", sorted(GOOD))
def test_every_type_validates_and_builds(name: str) -> None:
    option = build_option(make(name), LIGHT)
    json.dumps(option, allow_nan=False)


# -- Refusals: one per rule --------------------------------------------------------


class TestStructure:
    def test_not_an_object(self) -> None:
        assert "object" in refused(["bar"])

    def test_unknown_field(self) -> None:
        msg = refused(with_("bar", formatter="function(){}"))
        assert "unknown fields: formatter" in msg and "encoding" in msg

    def test_unknown_type(self) -> None:
        msg = refused(with_("bar", type="gauge"))
        assert "type must be one of" in msg and "boxplot" in msg

    def test_option_on_the_wrong_type(self) -> None:
        assert "donut applies to pie" in refused(with_("bar", donut=True))

    def test_option_not_true_or_false(self) -> None:
        assert "stacked must be true or false" in refused(with_("bar", stacked="yes"))

    def test_axis_title_on_a_type_without_axes(self) -> None:
        msg = refused(with_("pie", x_title="Region"))
        assert "x_title applies to" in msg and "bar" in msg

    def test_axis_title_too_long(self) -> None:
        assert "longer than 80" in refused(with_("bar", y_title="y" * 81))

    def test_axis_title_not_text(self) -> None:
        assert "must be text" in refused(with_("bar", y_title=5))

    def test_no_data(self) -> None:
        definition = with_("bar")
        del definition["data"]
        assert "needs data" in refused(definition)

    def test_data_not_an_object(self) -> None:
        assert "data must be an object" in refused(with_("bar", data=[1, 2]))

    def test_data_unknown_field(self) -> None:
        msg = refused(with_("bar", data={**SALES, "url": "https://example.com/x.csv"}))
        assert "unknown fields: url" in msg and "file_id" in msg

    def test_file_and_rows_together(self) -> None:
        assert "not both" in refused(with_("bar", data={**SALES, "file_id": 3}))

    @pytest.mark.parametrize("file_id", [0, -1, "3", True, 1.5])
    def test_bad_file_id(self, file_id: Any) -> None:
        assert "file id" in refused(with_("bar", data={"file_id": file_id}))

    def test_sheet_not_a_name(self) -> None:
        assert "sheet must be" in refused(with_("bar", data={"file_id": 3, "sheet": 1}))

    def test_sheet_without_a_file(self) -> None:
        assert "applies to a file" in refused(with_("bar", data={**SALES, "sheet": "S"}))

    @pytest.mark.parametrize("columns", [None, [], "Quarter", ["a", 1], ["a", " "]])
    def test_columns_not_a_list_of_names(self, columns: Any) -> None:
        assert "list of column names" in refused(
            with_("bar", data={"columns": columns, "rows": [[1]]})
        )

    def test_too_many_columns(self) -> None:
        cols = [f"c{i}" for i in range(51)]
        assert "at most 50" in refused(with_("bar", data=table(cols, [list(range(51))])))

    def test_duplicate_columns(self) -> None:
        assert "name twice" in refused(with_("bar", data=table(["a", "a"], [[1, 2]])))

    @pytest.mark.parametrize("rows", [None, [], "rows"])
    def test_no_rows(self, rows: Any) -> None:
        assert "data.rows must be a list" in refused(
            with_("bar", data={"columns": ["a"], "rows": rows})
        )

    def test_too_many_rows(self) -> None:
        rows = [["a", 1]] * 5001
        msg = refused(with_("bar", data=table(["x", "y"], rows), encoding={"x": "x", "y": "y"}))
        assert "at most 5,000" in msg

    def test_a_row_of_the_wrong_length(self) -> None:
        assert "Row 2 must be a list of 5 cells" in refused(
            with_("bar", data=table(SALES["columns"], [SALES["rows"][0], ["Q2", 1]]))
        )

    @pytest.mark.parametrize("cell", [{"a": 1}, [1], {"html": "<b>"}])
    def test_a_cell_that_is_not_text_or_a_number(self, cell: Any) -> None:
        rows = [["Q1", cell, 1, "N", 1]]
        assert "not text, a number or empty" in refused(
            with_("bar", data=table(SALES["columns"], rows))
        )


class TestEncoding:
    @pytest.mark.parametrize("encoding", [None, {}, "x"])
    def test_missing(self, encoding: Any) -> None:
        msg = refused(with_("bar", encoding=encoding))
        assert "encoding must say" in msg and "x, y, series" in msg

    def test_unknown_part(self) -> None:
        msg = refused(with_("bar", encoding={"x": "Quarter", "y": "Sales", "label": "Region"}))
        assert "unknown parts: label" in msg and "colour" in msg

    def test_a_part_the_type_does_not_use(self) -> None:
        msg = refused(with_("pie", encoding={"x": "Region", "y": "Sales", "size": "Sales"}))
        assert "does not use size" in msg

    @pytest.mark.parametrize("value", [5, "", None, {"col": "x"}])
    def test_not_a_column_name(self, value: Any) -> None:
        assert "must be a column name" in refused(with_("pie", encoding={"x": value, "y": "Sales"}))

    def test_several_columns_where_one_is_taken(self) -> None:
        assert "must be a column name" in refused(
            with_("pie", encoding={"x": "Region", "y": ["Sales", "North"]})
        )

    def test_a_required_part_missing(self) -> None:
        msg = refused(with_("sankey", encoding={"x": "From", "y": "To"}))
        assert "needs encoding.size" in msg and "flow value" in msg

    def test_several_y_and_series(self) -> None:
        msg = refused(
            with_("bar", encoding={"x": "Quarter", "y": ["North", "South"], "series": "Region"})
        )
        assert "not both" in msg

    def test_too_many_y_columns(self) -> None:
        cols = ["x"] + [f"v{i}" for i in range(9)]
        msg = refused(
            with_("bar", data=table(cols, [["a"] + [1] * 9]), encoding={"x": "x", "y": cols[1:]})
        )
        assert "at most 8" in msg


class TestData:
    def test_a_column_that_is_not_there(self) -> None:
        msg = refused(with_("bar", encoding={"x": "Month", "y": "Sales"}))
        assert "no column Month" in msg and "Quarter" in msg

    def test_text_where_numbers_are_needed(self) -> None:
        msg = refused(with_("pie", data=table(["Region", "Sales"], [["N", "lots"]])))
        assert "must hold numbers; row 1 has 'lots'" in msg

    def test_true_or_false_is_not_a_number(self) -> None:
        assert "true or false" in refused(
            with_("pie", data=table(["Region", "Sales"], [["N", True]]))
        )

    def test_a_number_that_is_not_finite(self) -> None:
        assert "finite" in refused(with_("pie", data=table(["Region", "Sales"], [["N", math.inf]])))

    def test_a_label_too_long(self) -> None:
        msg = refused(with_("pie", data=table(["Region", "Sales"], [["n" * 81, 1]])))
        assert "longer than 80" in msg

    @pytest.mark.parametrize("label", ["image://example.com/x.png", "PATH://M0 0L10 10"])
    def test_a_label_that_is_a_picture(self, label: str) -> None:
        msg = refused(with_("pie", data=table(["Region", "Sales"], [[label, 1]])))
        assert "image:// or path://" in msg

    def test_too_many_categories(self) -> None:
        rows = [[f"c{i}", i] for i in range(101)]
        msg = refused(with_("bar", data=table(["x", "y"], rows), encoding={"x": "x", "y": "y"}))
        assert "101 categories" in msg and "at most 100" in msg

    def test_too_many_series(self) -> None:
        rows = [["q", 1, f"s{i}"] for i in range(9)]
        msg = refused(
            with_(
                "line",
                data=table(["x", "y", "s"], rows),
                encoding={"x": "x", "y": "y", "series": "s"},
            )
        )
        assert "9 series" in msg

    def test_a_negative_slice(self) -> None:
        assert "zero or more" in refused(with_("pie", data=table(["Region", "Sales"], [["N", -1]])))

    def test_too_many_slices(self) -> None:
        rows = [[f"s{i}", 1] for i in range(9)]
        msg = refused(with_("pie", data=table(["Region", "Sales"], rows)))
        assert "9 slices" in msg and "Other" in msg

    def test_a_treemap_needs_sizes_above_zero(self) -> None:
        assert "more than zero" in refused(
            with_("treemap", data=table(["Item", "Size", "Group"], [["a", 0, "G"]]))
        )

    def test_too_many_rectangles(self) -> None:
        rows = [[f"i{i}", 1] for i in range(201)]
        msg = refused(
            {
                "type": "treemap",
                "data": table(["Item", "Size"], rows),
                "encoding": {"x": "Item", "y": "Size"},
            }
        )
        assert "at most 200" in msg

    def test_a_sankey_with_a_cycle(self) -> None:
        rows = [["a", "b", 1], ["b", "c", 1], ["c", "a", 1]]
        msg = refused(with_("sankey", data=table(["From", "To", "Amount"], rows)))
        assert "cycle" in msg and "graph" in msg

    def test_a_link_to_itself(self) -> None:
        msg = refused(
            with_("graph", data=table(["From", "To", "Amount", "Team"], [["a", "a", 1, "t"]]))
        )
        assert "to itself" in msg

    def test_a_link_value_of_zero(self) -> None:
        assert "more than zero" in refused(
            with_("sankey", data=table(["From", "To", "Amount"], [["a", "b", 0]]))
        )

    def test_too_many_nodes(self) -> None:
        rows = [[f"n{i}", f"m{i}", 1] for i in range(101)]
        msg = refused(with_("sankey", data=table(["From", "To", "Amount"], rows)))
        assert "202 nodes" in msg and "at most 200" in msg

    def test_too_many_links(self) -> None:
        rows = [[f"n{i}", f"n{j}"] for i in range(50) for j in range(i + 1, 50)][:1001]
        msg = refused(
            {
                "type": "graph",
                "data": table(["From", "To"], rows),
                "encoding": {"x": "From", "y": "To"},
            }
        )
        assert "links" in msg and "at most 1000" in msg

    def test_too_many_network_groups(self) -> None:
        rows = [[f"n{i}", "hub", 1, f"g{i}"] for i in range(9)]
        assert "9 groups" in refused(
            with_("graph", data=table(["From", "To", "Amount", "Team"], rows))
        )

    def test_a_radar_needs_three_measures(self) -> None:
        msg = refused(
            with_("radar", data=table(["Skill", "Ada", "Bo"], [["a", 1, 2], ["b", 1, 2]]))
        )
        assert "at least 3 measures" in msg

    def test_a_radar_value_below_zero(self) -> None:
        rows = [["a", 1, 2], ["b", -1, 2], ["c", 1, 2]]
        assert "zero or more" in refused(with_("radar", data=table(["Skill", "Ada", "Bo"], rows)))

    def test_a_point_size_below_zero(self) -> None:
        msg = refused(with_("scatter", data=table(["a", "b", "c", "g"], [[1, 2, -3, "x"]])))
        assert "zero or more" in msg

    def test_scatter_needs_numbers(self) -> None:
        assert (
            "no row has numbers"
            in refused(
                with_("scatter", data=table(["a", "b", "c", "g"], [[None, 2, 3, "x"]]))
            ).lower()
        )

    def test_too_many_heatmap_rows(self) -> None:
        rows = [["9", f"d{i}", 1] for i in range(101)]
        msg = refused(with_("heatmap", data=table(["Hour", "Day", "Calls"], rows)))
        assert "101 rows" in msg


class TestArtefactText:
    def test_caption_too_long(self) -> None:
        with pytest.raises(ChartRefused, match="longer than 500"):
            caption_text("c" * 501)

    def test_caption_not_text(self) -> None:
        with pytest.raises(ChartRefused, match="must be text"):
            caption_text(["c"])

    @pytest.mark.parametrize(
        "source",
        [
            "%%{init: {'flowchart': {'htmlLabels': true}}}%%\ngraph TD\nA-->B",
            "---\nconfig:\n  flowchart:\n    htmlLabels: true\n---\ngraph TD\nA-->B",
        ],
    )
    def test_a_diagram_may_not_change_mermaids_settings(self, source: str) -> None:
        with pytest.raises(ChartRefused, match="settings"):
            diagram_source(source)

    @pytest.mark.parametrize("source", [None, "", "  ", 5])
    def test_a_diagram_needs_its_source(self, source: Any) -> None:
        with pytest.raises(ChartRefused, match="Mermaid text"):
            diagram_source(source)

    def test_a_diagram_too_long(self) -> None:
        with pytest.raises(ChartRefused, match="longer than 20,000"):
            diagram_source("graph TD\n" + "A-->B\n" * 4000)

    def test_a_diagram_with_a_title_block_is_fine(self) -> None:
        text = "---\ntitle: Flow\n---\ngraph TD\nA-->B"
        assert diagram_source(text) == text


# -- The option per type -------------------------------------------------------------


def shape_of(value: Any) -> Any:
    """Keys and value types, with a list described by its first element."""
    if isinstance(value, dict):
        return {k: shape_of(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape_of(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__


@pytest.fixture(scope="module")
def recorded() -> Iterator[dict[str, Any]]:
    shapes: dict[str, Any] = (
        json.loads(SHAPES.read_text(encoding="utf-8")) if SHAPES.exists() else {}
    )
    yield shapes
    if os.environ.get("SPARK_UPDATE_CHART_SHAPES"):
        SHAPES.write_text(
            json.dumps(shapes, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )


@pytest.mark.parametrize("name", sorted(GOOD))
def test_the_option_shape_per_type(name: str, recorded: dict[str, Any]) -> None:
    got = {
        theme: shape_of(
            build_option(make(name), THEMES[theme], title="T", caption="C", export=export)
        )
        for theme, export in (("light", False), ("dark", True))
    }
    if os.environ.get("SPARK_UPDATE_CHART_SHAPES"):
        recorded[name] = got
    assert recorded.get(name) == got


def test_each_type_draws_its_own_series() -> None:
    expected = {
        "bar": "bar",
        "line": "line",
        "area": "line",
        "scatter": "scatter",
        "pie": "pie",
        "heatmap": "heatmap",
        "treemap": "treemap",
        "sankey": "sankey",
        "graph": "graph",
        "radar": "radar",
        "funnel": "funnel",
        "boxplot": "boxplot",
    }
    for name, series in expected.items():
        assert build_option(make(name), LIGHT)["series"][0]["type"] == series, name


def test_the_values_are_the_datas() -> None:
    bar = build_option(make("bar"), LIGHT)
    assert bar["xAxis"]["data"] == ["Q1", "Q2", "Q3", "Q4"]
    assert [s["data"] for s in bar["series"]] == [[10, 14, 18, 12], [12, 9, 15, 20]]
    assert all(s["stack"] == "total" for s in bar["series"])
    line = build_option(make("line"), LIGHT)
    assert {s["name"]: s["data"] for s in line["series"]} == {
        "North": [10, None, 18, None],
        "South": [None, 9, None, 20],
    }
    pie = build_option(make("pie"), LIGHT)
    assert pie["series"][0]["data"] == [
        {"name": "North", "value": 28},
        {"name": "South", "value": 29},
    ]
    assert pie["series"][0]["radius"] == ["45%", "70%"]
    box = build_option(make("boxplot"), LIGHT)
    assert box["series"][0]["data"][0] == [1, 2, 3, 4, 4]
    assert box["series"][1]["data"] == [[0, 40]]
    graph = build_option(make("graph"), LIGHT)["series"][0]
    assert [c["name"] for c in graph["categories"]] == [
        "Marketing",
        "Product",
        "Sales",
        "Ungrouped",
    ]
    assert graph["label"]["show"] is True


def test_horizontal_bars_swap_the_axes() -> None:
    option = build_option(prepare(*_spec(with_("bar", horizontal=True))), LIGHT)
    assert option["yAxis"]["type"] == "category" and option["xAxis"]["type"] == "value"


def _spec(definition: dict[str, Any]) -> tuple[Any, Any]:
    spec = validate(definition)
    return spec, spec.data


def test_network_labels_show_on_hover_above_forty_nodes() -> None:
    rows = [[f"n{i}", f"n{i + 1}", 1] for i in range(45)]
    for kind in ("sankey", "graph"):
        definition = {
            "type": kind,
            "data": table(["a", "b", "w"], rows),
            "encoding": {"x": "a", "y": "b", "size": "w"},
        }
        series = build_option(prepare(*_spec(definition)), DARK)["series"][0]
        assert series["label"]["show"] is False
        assert series["emphasis"]["label"]["show"] is True


def test_an_export_draws_the_title_on_its_background() -> None:
    option = build_option(make("bar"), LIGHT, title="Sales", caption="By quarter", export=True)
    assert option["title"]["text"] == "Sales" and option["title"]["subtext"] == "By quarter"
    assert option["backgroundColor"] == "#ffffff" and option["animation"] is False
    page = build_option(make("bar"), DARK, title="Sales")
    assert "title" not in page and page["backgroundColor"] == "transparent"
    assert page["aria"]["label"]["description"] == "Sales"


# -- Data only ----------------------------------------------------------------------

HOSTILE = [
    "<img src=x onerror=alert(1)>",
    "<script>alert(1)</script>",
    "function(){alert(1)}",
    "{a|x}{b}",
    "javascript:alert(1)",
    "https://example.com/x.png",
    "line\none\x00‮",
]


def _hostile(name: str, payload: str) -> dict[str, Any]:
    """GOOD[name] with ``payload`` in every text field: column names, every text cell,
    and the axis titles."""
    definition = json.loads(json.dumps(GOOD[name]))
    data = definition["data"]
    rename = {c: f"{c} {payload}" for c in data["columns"]}
    data["columns"] = [rename[c] for c in data["columns"]]
    data["rows"] = [
        [f"{v} {payload}" if isinstance(v, str) else v for v in row] for row in data["rows"]
    ]
    definition["encoding"] = {
        k: ([rename[c] for c in v] if isinstance(v, list) else rename[v])
        for k, v in definition["encoding"].items()
    }
    for key in ("x_title", "y_title"):
        if key in definition:
            definition[key] = f"T {payload}"
    return definition


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, f"{path}[]")
    elif isinstance(value, str):
        yield path, value
    else:
        assert value is None or isinstance(value, (int, float, bool)), (path, value)


@pytest.mark.parametrize("payload_text", HOSTILE)
@pytest.mark.parametrize("name", sorted(GOOD))
def test_no_function_markup_picture_or_link_reaches_the_option(
    name: str, payload_text: str
) -> None:
    prepared = prepare(*_spec(_hostile(name, payload_text)))
    for theme in (LIGHT, DARK):
        option = build_option(prepared, theme, title="T", caption="C", export=True)
        json.dumps(option, allow_nan=False)
        for path, text in _strings(option):
            assert "<" not in text and ">" not in text, path
            assert "\n" not in text and "\x00" not in text and "‮" not in text, path
            key = path.rsplit(".", 1)[-1].rstrip("[]")
            assert key not in ("rich", "link", "sublink", "image", "html", "symbol"), path
            if key == "formatter":
                assert text in FORMATTERS, path
            assert not text.lower().startswith(("image://", "path://")), path


@pytest.mark.parametrize("name", sorted(GOOD))
def test_every_colour_is_the_themes(name: str) -> None:
    for theme in (LIGHT, DARK):
        option = build_option(make(name), theme, title="T", export=True)
        for path, text in _strings(option):
            if text.startswith(("#", "rgb", "hsl")):
                assert text in theme.colours(), (name, theme.name, path, text)


def test_the_palette_is_the_colour_blind_safe_one() -> None:
    okabe_ito = {"#e69f00", "#56b4e9", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7"}
    for theme in (LIGHT, DARK):
        assert len(theme.series) == schema.LIMITS.series
        assert len(okabe_ito & set(theme.series)) >= 6


# -- Stored artefacts ---------------------------------------------------------------


def test_a_chart_round_trips_and_is_checked_again() -> None:
    chart = Chart("Sales", "By quarter", make("bar"))
    raw = chart_document(chart)
    back = read_artefact(raw)
    assert isinstance(back, Chart)
    assert back.prepared.table == chart.prepared.table
    assert payload(back, DARK)["option"] == payload(chart, DARK)["option"]
    body = json.loads(raw)
    body["definition"]["type"] = "gauge"
    with pytest.raises(ChartRefused):
        read_artefact(json.dumps(body).encode())
    with pytest.raises(ChartRefused):
        read_artefact(b"not json")


def test_a_diagram_round_trips() -> None:
    raw = diagram_document(Diagram("Flow", "", "graph TD\nA-->B"))
    back = read_artefact(raw)
    assert isinstance(back, Diagram) and back.source == "graph TD\nA-->B"
    assert payload(back, LIGHT)["kind"] == "diagram"


def test_csv_of_the_snapshot_runs_no_formula() -> None:
    rows = [["=HYPERLINK(1)", 3], ["+cmd", 4], ["Plain", -2.5]]
    definition = {
        "type": "bar",
        "data": table(["Name", "Value"], rows),
        "encoding": {"x": "Name", "y": "Value"},
    }
    chart = Chart("T", "", prepare(*_spec(definition)))
    lines = csv_bytes(chart).decode("utf-8-sig").splitlines()
    assert lines == ["Name,Value", "'=HYPERLINK(1),3", "'+cmd,4", "Plain,-2.5"]


def test_the_preview_knows_charts_and_diagrams() -> None:
    from spark.charts.artefact import CHART_MIME, DIAGRAM_MIME
    from spark.preview.kinds import Kind, classify, content_matches, describe_type

    assert classify(CHART_MIME, "x.chart.json") == (Kind.CHART, CHART_MIME)
    assert classify(DIAGRAM_MIME, "x.diagram.json") == (Kind.DIAGRAM, DIAGRAM_MIME)
    assert describe_type(Kind.CHART) == "chart" and describe_type(Kind.DIAGRAM) == "diagram"
    raw = chart_document(Chart("T", "", make("pie")))
    assert content_matches(Kind.CHART, CHART_MIME, raw)
    assert not content_matches(Kind.CHART, CHART_MIME, b"%PDF-1.4")
    assert not content_matches(Kind.DIAGRAM, DIAGRAM_MIME, b"\x00{")


def test_the_tool_documentation_covers_the_schema() -> None:
    from dataclasses import asdict

    docs = Path(schema.__file__).parents[1] / "resources" / "tool_docs" / "create_chart.md"
    text = docs.read_text(encoding="utf-8")
    for word in (*schema.CHART_TYPES, *schema.ENCODINGS, *schema.FIELDS, *schema.DATA_FIELDS):
        assert f"`{word}`" in text or f"| {word} |" in text, word
    for name, value in asdict(schema.LIMITS).items():
        if name == "labels_on_hover_above":
            assert f"more than {value} nodes" in text
        elif name == "indicators_min":
            assert f"{value} to {schema.LIMITS.indicators_max}" in text
        elif name != "indicators_max":
            assert f"{value:,}" in text, name
