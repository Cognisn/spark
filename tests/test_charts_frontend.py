"""charts.js and diagrams.js under Node, and the chart pages' static guarantees.

* charts.js (its logic, under Node): file names from titles (no path, reserved or
  markup characters, never empty), the chart API's URLs, the toolbar's actions per
  kind (no Open in preview in the pane, Download data for charts only) and the theme a
  saved image takes (white unless Match theme in the dark theme).
* diagrams.js: one Mermaid configuration, at the strict level with HTML labels off,
  listing the settings a diagram's directives may never change.
* No CDN: the chat page loads Mermaid from SPARK, nothing loads ECharts or Mermaid from
  a CDN, and the chart scripts and styles name no external host.
* What is drawn comes from the chart API only: charts.js and the frame draw
  ``payload.option`` and ``payload.source`` and fetch nothing but ``/charts/api``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_preview_frontend import CDN_HOSTS

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
JS = WEB / "static" / "js"
TEMPLATES = WEB / "templates"
CHART_FILES = [JS / "charts.js", JS / "diagrams.js", WEB / "static" / "css" / "charts.css"]

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not installed")

_SCRIPT = r"""
const charts = require(process.argv[1]);
const diagrams = require(process.argv[2]);
const out = {
  names: [
    charts.fileName('Units sold by quarter', 'png'),
    charts.fileName('  a/b\\c:d*e?f"g<h>i|j\u0001 ', 'svg'),
    charts.fileName('', 'csv'),
    charts.fileName('...', 'png'),
    charts.fileName('Sales ＜Q1＞', 'png'),
    charts.fileName('x'.repeat(150), 'png').length,
  ],
  view: [charts.viewUrl(7, 'dark', false), charts.viewUrl(7, 'evil', true)],
  data: charts.dataUrl(7),
  actions: [charts.actions('chart', false), charts.actions('chart', true),
            charts.actions('diagram', false), charts.actions('diagram', true)],
  exportTheme: [charts.exportTheme(false, 'dark'), charts.exportTheme(true, 'dark'),
                charts.exportTheme(true, 'light'), charts.exportTheme(false, 'light')],
  artefact: [charts.isArtefact({kind: 'chart'}), charts.isArtefact({kind: 'diagram'}),
             charts.isArtefact({kind: 'pdf'}), charts.isArtefact(null)],
  sizes: [charts.HEIGHT, charts.PIXEL_RATIO, charts.EXPORT_SIZE],
  light: diagrams.config('light'),
  dark: diagrams.config('dark'),
  odd: diagrams.config('<script>'),
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def node() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("Node is not installed")
    done = subprocess.run(
        [NODE, "-e", _SCRIPT, str(JS / "charts.js"), str(JS / "diagrams.js")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result: dict[str, Any] = json.loads(done.stdout)
    return result


@needs_node
def test_file_names_come_from_the_title(node: dict[str, Any]) -> None:
    names = node["names"]
    assert names[0] == "Units sold by quarter.png"
    assert names[1] == "a b c d e f g h i j.svg"
    assert names[2] == "chart.csv" and names[3] == "chart.png"
    assert names[4] == "Sales Q1.png"
    assert names[5] == 104


@needs_node
def test_the_chart_api_urls(node: dict[str, Any]) -> None:
    assert node["view"] == ["/charts/api/7?theme=dark", "/charts/api/7?theme=light&export=1"]
    assert node["data"] == "/charts/api/7/data.csv"


@needs_node
def test_the_toolbar_actions(node: dict[str, Any]) -> None:
    assert node["actions"] == [
        ["open", "png", "svg", "csv"],
        ["png", "svg", "csv"],
        ["open", "png", "svg"],
        ["png", "svg"],
    ]


@needs_node
def test_saved_images_are_on_white_unless_matching_the_dark_theme(node: dict[str, Any]) -> None:
    assert node["exportTheme"] == ["light", "dark", "light", "light"]
    assert node["sizes"][0] == 320 and node["sizes"][1] >= 2


@needs_node
def test_artefact_kinds(node: dict[str, Any]) -> None:
    assert node["artefact"] == [True, True, False, False]


@needs_node
def test_mermaid_is_strict_with_html_labels_off(node: dict[str, Any]) -> None:
    for config, theme in (
        (node["light"], "default"),
        (node["dark"], "dark"),
        (node["odd"], "default"),
    ):
        assert config["securityLevel"] == "strict"
        assert config["htmlLabels"] is False
        assert config["flowchart"] == {"htmlLabels": False}
        assert config["startOnLoad"] is False
        assert config["theme"] == theme
        assert {
            "securityLevel",
            "htmlLabels",
            "flowchart",
            "secure",
            "theme",
            "themeCSS",
            "dompurifyConfig",
        } <= set(config["secure"])


class TestNoCdn:
    def test_the_chat_page_loads_mermaid_from_spark(self) -> None:
        chat = (TEMPLATES / "chat.html").read_text(encoding="utf-8")
        assert '<script src="/static/vendor/mermaid/mermaid.min.js"></script>' in chat
        assert "mermaid@" not in chat and "npm/mermaid" not in chat
        assert chat.index("/static/js/diagrams.js") < chat.index("/static/js/main.js")
        assert chat.index("/static/js/charts.js") < chat.index("/static/js/chat.js")

    def test_no_template_loads_echarts_or_mermaid_from_a_cdn(self) -> None:
        for path in TEMPLATES.glob("*.html"):
            for tag in re.findall(r"<(?:script|link)\b[^>]*>", path.read_text(encoding="utf-8")):
                if CDN_HOSTS.search(tag):
                    assert "mermaid" not in tag.lower() and "echarts" not in tag.lower(), (
                        path.name,
                        tag,
                    )

    def test_the_chart_files_name_no_external_host(self) -> None:
        for path in CHART_FILES:
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", text), path.name
            assert not re.search(r"""(import|fetch)\s*\(\s*["'`](https?:)?//""", text), path.name
            assert not CDN_HOSTS.search(text), path.name


class TestDrawnFromTheApi:
    def test_charts_js_draws_only_the_apis_payload(self) -> None:
        text = (JS / "charts.js").read_text(encoding="utf-8")
        assert re.findall(r"setOption\(([^,)]+)", text) == ["payload.option", "payload.option"]
        assert re.findall(r"fetch\((\w+)", text) == ["viewUrl"]
        assert "eval(" not in text and "new Function" not in text and "innerHTML" not in text

    def test_the_frame_draws_only_the_payload(self) -> None:
        text = (JS / "preview_frame.js").read_text(encoding="utf-8")
        assert "chart.setOption(payload.option)" in text
        assert "SparkDiagrams.render(holder, payload.source, doc.theme)" in text
        assert "fetch(" not in text

    def test_diagrams_insert_only_mermaids_svg(self) -> None:
        text = (JS / "diagrams.js").read_text(encoding="utf-8")
        assert re.findall(r"innerHTML\s*=\s*([^;]+);", text) == ["result.svg"]
