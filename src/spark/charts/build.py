"""A checked chart definition to an ECharts option, on the server.

:func:`build_option` turns a :class:`~spark.charts.validate.Prepared` chart into the
option the page hands to ECharts. The option is data only: numbers, plain text from
the definition (already neutralised) and the builder's own constant settings. No
function, formatter written by the model, rich text, HTML, URL or picture can be in
it, and every colour comes from the theme (:mod:`spark.charts.theme`).

Readable by default: labels hide where they would overlap, long category labels are
truncated on the axis (the tooltip shows them whole), legends scroll, and network and
Sankey charts show node labels on hover only above
:attr:`~spark.charts.schema.Limits.labels_on_hover_above` nodes. The network and
Sankey designs follow URAVI's communication flow charts (node size by the square
root of weight, link width by the square root of value, adjacency focus on hover),
written fresh for SPARK.
"""

from __future__ import annotations

import math
from typing import Any, Final

from spark.charts.schema import LIMITS
from spark.charts.theme import FONT, Theme
from spark.charts.validate import (
    BoxShape,
    CategoryShape,
    FlowShape,
    GridShape,
    PointShape,
    Prepared,
    SliceShape,
    TreeShape,
)
from spark.files.titles import clean_text

__all__ = ["FORMATTERS", "build_option"]

# The only label formatters an option ever holds: ECharts templates, fixed here.
FORMATTERS: Final = frozenset({"{b}: {d}%", "{b}: {c}", "{b}"})
_AXIS_LABEL_WIDTH: Final = 120


def _text_style(theme: Theme) -> dict[str, Any]:
    return {"color": theme.text, "fontFamily": FONT}


def _tooltip(theme: Theme, trigger: str) -> dict[str, Any]:
    return {
        "trigger": trigger,
        "confine": True,
        "backgroundColor": theme.surface,
        "borderColor": theme.border,
        "textStyle": {"color": theme.text, "fontFamily": FONT},
    }


def _legend(theme: Theme, names: list[str]) -> dict[str, Any]:
    return {
        "type": "scroll",
        "bottom": 0,
        "data": names,
        "textStyle": {"color": theme.text},
        "inactiveColor": theme.muted,
        "pageTextStyle": {"color": theme.muted},
        "pageIconColor": theme.text,
        "pageIconInactiveColor": theme.muted,
    }


def _axis_name(name: str, theme: Theme, horizontal: bool) -> dict[str, Any]:
    if not name:
        return {}
    return {
        "name": name,
        "nameLocation": "middle",
        "nameGap": 34 if horizontal else 44,
        "nameTextStyle": {"color": theme.muted},
    }


def _category_axis(names: list[str], title: str, theme: Theme, horizontal: bool) -> dict[str, Any]:
    return {
        "type": "category",
        "data": names,
        "axisLine": {"lineStyle": {"color": theme.border}},
        "axisTick": {"lineStyle": {"color": theme.border}},
        "axisLabel": {
            "color": theme.muted,
            "hideOverlap": True,
            "width": _AXIS_LABEL_WIDTH,
            "overflow": "truncate",
            "rotate": 0 if horizontal or len(names) <= 12 else 30,
        },
        **_axis_name(title, theme, horizontal),
    }


def _value_axis(title: str, theme: Theme, horizontal: bool, scale: bool = False) -> dict[str, Any]:
    return {
        "type": "value",
        "scale": scale,
        "axisLine": {"show": False},
        "axisLabel": {"color": theme.muted, "hideOverlap": True},
        "splitLine": {"lineStyle": {"color": theme.grid}},
        **_axis_name(title, theme, horizontal),
    }


def _grid(legend: bool) -> dict[str, Any]:
    return {
        "left": 16,
        "right": 24,
        "top": 24,
        "bottom": 40 if legend else 16,
        "containLabel": True,
    }


def _number(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() and abs(value) < 2**53 else round(value, 6)


def _categorical(prepared: Prepared, shape: CategoryShape, theme: Theme) -> dict[str, Any]:
    spec = prepared.spec
    many = len(shape.series) > 1
    names = [n for n, _ in shape.series]
    series: list[dict[str, Any]] = []
    for name, values in shape.series:
        item: dict[str, Any] = {
            "name": name,
            "type": "bar" if spec.type == "bar" else "line",
            "data": [_number(v) for v in values],
            "emphasis": {"focus": "series"},
        }
        if spec.stacked:
            item["stack"] = "total"
        if spec.type != "bar":
            item["showSymbol"] = len(shape.categories) <= LIMITS.labels_on_hover_above
            item["symbolSize"] = 6
            item["connectNulls"] = False
        if spec.type == "area":
            item["areaStyle"] = {"opacity": 0.45 if spec.stacked else 0.2}
        series.append(item)
    horizontal = spec.horizontal
    category = _category_axis(list(shape.categories), spec.x_title, theme, horizontal)
    value = _value_axis(spec.y_title, theme, horizontal)
    option: dict[str, Any] = {
        "tooltip": {**_tooltip(theme, "axis"), "axisPointer": {"type": "shadow"}},
        "grid": _grid(many),
        "xAxis": value if horizontal else category,
        "yAxis": category if horizontal else value,
        "series": series,
    }
    if spec.type != "bar":
        option["tooltip"]["axisPointer"] = {"type": "line"}
        option["xAxis"]["boundaryGap"] = spec.type == "bar"
    if many:
        option["legend"] = _legend(theme, names)
    return option


def _radar(shape: CategoryShape, theme: Theme) -> dict[str, Any]:
    peak = max((v for _, values in shape.series for v in values if v is not None), default=0.0)
    top = peak * 1.1 if peak > 0 else 1
    names = [n for n, _ in shape.series]
    option: dict[str, Any] = {
        "tooltip": _tooltip(theme, "item"),
        "radar": {
            "indicator": [{"name": c, "max": _number(top)} for c in shape.categories],
            "axisName": {"color": theme.muted},
            "axisLine": {"lineStyle": {"color": theme.grid}},
            "splitLine": {"lineStyle": {"color": theme.grid}},
            "splitArea": {"show": False},
            "radius": "65%",
        },
        "series": [
            {
                "type": "radar",
                "emphasis": {"focus": "self"},
                "areaStyle": {"opacity": 0.15},
                "data": [
                    {"name": name, "value": [_number(v) or 0 for v in values]}
                    for name, values in shape.series
                ],
            }
        ],
    }
    if len(names) > 1:
        option["legend"] = _legend(theme, names)
    return option


def _scatter(prepared: Prepared, shape: PointShape, theme: Theme) -> dict[str, Any]:
    spec = prepared.spec
    sizes = [p[2] for _, points in shape.series for p in points if p[2] is not None]
    largest = max(sizes, default=0.0)

    def symbol(size: float | None) -> float:
        if size is None or largest <= 0:
            return 10
        return round(6 + 34 * math.sqrt(size / largest), 2)

    series = [
        {
            "name": name,
            "type": "scatter",
            "emphasis": {"focus": "series"},
            "data": [
                {
                    "value": [_number(x), _number(y)] + ([_number(s)] if shape.sized else []),
                    "symbolSize": symbol(s),
                }
                for x, y, s in points
            ],
        }
        for name, points in shape.series
    ]
    many = len(series) > 1
    option: dict[str, Any] = {
        "tooltip": _tooltip(theme, "item"),
        "grid": _grid(many),
        "xAxis": _value_axis(spec.x_title, theme, False, scale=True),
        "yAxis": _value_axis(spec.y_title, theme, False, scale=True),
        "series": series,
    }
    option["xAxis"]["splitLine"] = {"lineStyle": {"color": theme.grid}}
    if many:
        option["legend"] = _legend(theme, [n for n, _ in shape.series])
    return option


def _slices(prepared: Prepared, shape: SliceShape, theme: Theme) -> dict[str, Any]:
    spec = prepared.spec
    data = [{"name": n, "value": _number(v)} for n, v in shape.items]
    label = {"color": theme.text, "textBorderColor": theme.background, "textBorderWidth": 2}
    if spec.type == "funnel":
        series: dict[str, Any] = {
            "type": "funnel",
            "sort": "descending",
            "gap": 2,
            "left": "10%",
            "width": "80%",
            "top": 16,
            "bottom": 40,
            "label": {**label, "position": "inside", "formatter": "{b}: {c}"},
            "itemStyle": {"borderColor": theme.background, "borderWidth": 1},
            "data": data,
        }
    else:
        series = {
            "type": "pie",
            "radius": ["45%", "70%"] if spec.donut else "70%",
            "center": ["50%", "46%"],
            "avoidLabelOverlap": True,
            "label": {**label, "formatter": "{b}: {d}%"},
            "labelLine": {"lineStyle": {"color": theme.muted}},
            "itemStyle": {"borderColor": theme.background, "borderWidth": 1},
            "data": data,
        }
    return {
        "tooltip": _tooltip(theme, "item"),
        "legend": _legend(theme, [n for n, _ in shape.items]),
        "series": [series],
    }


def _heatmap(prepared: Prepared, shape: GridShape, theme: Theme) -> dict[str, Any]:
    spec = prepared.spec
    values = [v for _, _, v in shape.cells]
    low, high = min(values), max(values)
    return {
        "tooltip": {**_tooltip(theme, "item"), "position": "top"},
        "grid": {**_grid(True), "bottom": 56},
        "xAxis": {
            **_category_axis(list(shape.xs), spec.x_title, theme, False),
            "splitArea": {"show": False},
        },
        "yAxis": {
            **_category_axis(list(shape.ys), spec.y_title, theme, True),
            "splitArea": {"show": False},
        },
        "visualMap": {
            "min": _number(low),
            "max": _number(high if high > low else low + 1),
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 0,
            "itemHeight": 120,
            "inRange": {"color": list(theme.ramp)},
            "textStyle": {"color": theme.muted},
        },
        "series": [
            {
                "type": "heatmap",
                "data": [[x, y, _number(v)] for x, y, v in shape.cells],
                "label": {
                    "show": len(shape.cells) <= 100,
                    "color": theme.text,
                    "textBorderColor": theme.background,
                    "textBorderWidth": 2,
                },
                "itemStyle": {"borderColor": theme.background, "borderWidth": 1},
                "emphasis": {"itemStyle": {"borderColor": theme.text, "borderWidth": 1}},
            }
        ],
    }


def _treemap(shape: TreeShape, theme: Theme) -> dict[str, Any]:
    label = {"color": theme.text, "textBorderColor": theme.background, "textBorderWidth": 2}
    data: list[dict[str, Any]] = []
    for index, (group, items) in enumerate(shape.groups):
        leaves = [{"name": n, "value": _number(v)} for n, v in items]
        if group is None:
            data.extend(leaves)
        else:
            colour = theme.series[index % len(theme.series)]
            data.append({"name": group, "children": leaves, "itemStyle": {"color": colour}})
    return {
        "tooltip": _tooltip(theme, "item"),
        "series": [
            {
                "type": "treemap",
                "roam": False,
                "nodeClick": False,
                "breadcrumb": {"show": False},
                "top": 8,
                "bottom": 8,
                "left": 8,
                "right": 8,
                "label": {**label, "formatter": "{b}"},
                "upperLabel": {"show": shape.groups[0][0] is not None, "height": 22, **label},
                "itemStyle": {"borderColor": theme.background, "borderWidth": 1, "gapWidth": 1},
                "levels": [
                    {
                        "itemStyle": {
                            "borderColor": theme.background,
                            "borderWidth": 2,
                            "gapWidth": 2,
                        }
                    },
                    {
                        "itemStyle": {
                            "borderColor": theme.background,
                            "borderWidth": 1,
                            "gapWidth": 1,
                        }
                    },
                ],
                "data": data,
            }
        ],
    }


def _sankey(shape: FlowShape, theme: Theme) -> dict[str, Any]:
    labelled = len(shape.nodes) <= LIMITS.labels_on_hover_above
    return {
        "tooltip": _tooltip(theme, "item"),
        "series": [
            {
                "type": "sankey",
                "left": 8,
                "right": 120 if labelled else 8,
                "top": 8,
                "bottom": 8,
                "nodeGap": 8,
                "nodeAlign": "justify",
                "layoutIterations": 32,
                "emphasis": {"focus": "adjacency", "label": {"show": True}},
                "label": {
                    "show": labelled,
                    "color": theme.text,
                    "textBorderColor": theme.background,
                    "textBorderWidth": 2,
                },
                "lineStyle": {"color": "gradient", "curveness": 0.5, "opacity": 0.35},
                "data": [
                    {
                        "name": n,
                        "itemStyle": {
                            "color": theme.series[i % len(theme.series)],
                            "borderColor": theme.background,
                        },
                    }
                    for i, n in enumerate(shape.nodes)
                ],
                "links": [
                    {"source": s, "target": t, "value": _number(v)} for s, t, v in shape.links
                ],
            }
        ],
    }


def _graph(shape: FlowShape, theme: Theme) -> dict[str, Any]:
    degree: dict[str, float] = {n: 0.0 for n in shape.nodes}
    for source, target, value in shape.links:
        degree[source] += value
        degree[target] += value
    heaviest = max(degree.values(), default=1.0) or 1.0
    groups = list(dict.fromkeys(shape.groups.values()))
    if shape.groups and any(n not in shape.groups for n in shape.nodes):
        groups.append("Ungrouped")
    labelled = len(shape.nodes) <= LIMITS.labels_on_hover_above
    nodes: list[dict[str, Any]] = []
    for node in shape.nodes:
        item: dict[str, Any] = {
            "name": node,
            "value": _number(degree[node]),
            "symbolSize": round(10 + 30 * math.sqrt(degree[node] / heaviest), 2),
        }
        if groups:
            item["category"] = groups.index(shape.groups.get(node, "Ungrouped"))
        else:
            item["itemStyle"] = {"color": theme.series[0]}
        nodes.append(item)
    widest = max((v for _, _, v in shape.links), default=1.0) or 1.0
    option: dict[str, Any] = {
        "tooltip": _tooltip(theme, "item"),
        "series": [
            {
                "type": "graph",
                "layout": "force",
                "roam": True,
                "draggable": True,
                "force": {"repulsion": 160, "gravity": 0.08, "edgeLength": [40, 140]},
                "emphasis": {"focus": "adjacency", "label": {"show": True}},
                "label": {
                    "show": labelled,
                    "position": "right",
                    "color": theme.text,
                    "textBorderColor": theme.background,
                    "textBorderWidth": 2,
                },
                "edgeSymbol": ["none", "arrow"],
                "edgeSymbolSize": 6,
                "lineStyle": {"color": theme.muted, "opacity": 0.6, "curveness": 0.1},
                "itemStyle": {"borderColor": theme.background, "borderWidth": 1},
                "data": nodes,
                "links": [
                    {
                        "source": s,
                        "target": t,
                        "value": _number(v),
                        "lineStyle": {"width": round(1 + 5 * math.sqrt(v / widest), 2)},
                    }
                    for s, t, v in shape.links
                ],
            }
        ],
    }
    if groups:
        option["series"][0]["categories"] = [{"name": g} for g in groups]
        option["legend"] = _legend(theme, groups)
    return option


def _boxplot(prepared: Prepared, shape: BoxShape, theme: Theme) -> dict[str, Any]:
    spec = prepared.spec
    names = [n for n, _, _ in shape.groups]
    return {
        "tooltip": _tooltip(theme, "item"),
        "grid": _grid(False),
        "xAxis": _category_axis(names, spec.x_title, theme, False),
        "yAxis": _value_axis(spec.y_title, theme, False, scale=True),
        "series": [
            {
                "name": clean_text(spec.column("y"), limit=LIMITS.label),
                "type": "boxplot",
                "itemStyle": {
                    "color": theme.surface,
                    "borderColor": theme.series[0],
                    "borderWidth": 1.5,
                },
                "data": [[_number(v) for v in box] for _, box, _ in shape.groups],
            },
            {
                "name": "Outliers",
                "type": "scatter",
                "itemStyle": {"color": theme.series[1]},
                "symbolSize": 7,
                "data": [
                    [i, _number(v)] for i, (_, _, out) in enumerate(shape.groups) for v in out
                ],
            },
        ],
    }


def _body(prepared: Prepared, theme: Theme) -> dict[str, Any]:
    shape = prepared.shape
    if isinstance(shape, CategoryShape):
        if prepared.spec.type == "radar":
            return _radar(shape, theme)
        return _categorical(prepared, shape, theme)
    if isinstance(shape, PointShape):
        return _scatter(prepared, shape, theme)
    if isinstance(shape, SliceShape):
        return _slices(prepared, shape, theme)
    if isinstance(shape, GridShape):
        return _heatmap(prepared, shape, theme)
    if isinstance(shape, TreeShape):
        return _treemap(shape, theme)
    if isinstance(shape, FlowShape):
        return _sankey(shape, theme) if prepared.spec.type == "sankey" else _graph(shape, theme)
    return _boxplot(prepared, shape, theme)


def build_option(
    prepared: Prepared,
    theme: Theme,
    *,
    title: str = "",
    caption: str = "",
    export: bool = False,
) -> dict[str, Any]:
    """The ECharts option for ``prepared`` in ``theme``.

    ``title`` and ``caption`` (already cleaned) describe the chart for screen readers;
    with ``export`` they are drawn above it, on the theme's background, for a saved
    image, and animation is off.
    """
    option = _body(prepared, theme)
    description = ". ".join(t for t in (title, caption) if t)
    option.update(
        {
            "backgroundColor": theme.background if export else "transparent",
            "color": list(theme.series),
            "textStyle": _text_style(theme),
            "animation": not export,
            "aria": {"enabled": True, "label": {"description": description}},
        }
    )
    if export and title:
        option["title"] = {
            "text": title,
            "subtext": caption,
            "left": "center",
            "top": 8,
            "textStyle": {"color": theme.text, "fontFamily": FONT, "fontSize": 18},
            "subtextStyle": {"color": theme.muted, "fontFamily": FONT},
        }
        if "grid" in option:
            option["grid"]["top"] = 72 if caption else 56
        for series in option["series"]:
            if series.get("type") in ("pie",):
                series["center"] = ["50%", "56%"]
            if series.get("type") in ("treemap", "sankey", "funnel"):
                series["top"] = 72 if caption else 56
        if "radar" in option:
            option["radar"]["center"] = ["50%", "58%"]
    return option
