"""Charts and diagrams the model makes, drawn by SPARK (PC19, PC20).

* :mod:`spark.charts.schema`: the chart definition's closed vocabulary and its limits.
* :mod:`spark.charts.validate`: checking a definition and shaping its data.
* :mod:`spark.charts.data`: data from a CSV or Excel file of the conversation.
* :mod:`spark.charts.theme`: SPARK's light and dark palettes.
* :mod:`spark.charts.build`: the ECharts option, built on the server, data only.
* :mod:`spark.charts.artefact`: charts and diagrams as stored files, and what the page
  is given to draw them.

The tools are :mod:`spark.tools.charts` (``create_chart``) and
:mod:`spark.tools.diagrams` (``create_diagram``); the page reads
``GET /charts/api/<file id>`` (:mod:`spark.web.endpoints.charts`).
"""

from __future__ import annotations

from spark.charts.schema import CHART_TYPES, ENCODINGS, FIELDS, LIMITS, TYPES, describe
from spark.charts.validate import ChartRefused

__all__ = ["CHART_TYPES", "ENCODINGS", "FIELDS", "LIMITS", "TYPES", "ChartRefused", "describe"]
