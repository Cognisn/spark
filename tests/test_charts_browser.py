"""Charts and diagrams in a real browser: drawing, the toolbar, the pane and the Mermaid setting.

A headless Chromium opens a harness page with the real chat scripts (markdown.js,
main.js, diagrams.js, the vendored Mermaid, preview.js, charts.js and chat.js), the real
preview pane and the real frame under its Content Security Policy. The harness serves
``/charts/api/<id>`` from the real builder (:func:`spark.charts.artefact.payload`) for
one chart of every type and a diagram, and the page reports back on the real clock
(decision 215), so a loaded machine only waits longer.

* Every card arrives through chat.js (the history's tool results and a live result)
  and draws at its fixed height: an ECharts instance with the type's series, or the
  diagram's SVG with no HTML labels (no ``foreignObject``).
* Mermaid runs at the strict security level with HTML labels off, everywhere: the
  page's own configuration, a diagram written in an ordinary reply, and a stored
  diagram; a hostile label never runs.
* Save as PNG gives a 2x image (2400 by 1440) on white, or on the dark background
  with Match theme; Save as SVG gives ECharts' SVG with the title; a diagram saves as
  SVG and PNG with its title; Download data links the CSV; file names come from the
  title.
* Open in preview draws the chart and the diagram in the sandboxed frame.
* The toolbar is a labelled toolbar of native buttons; the arrow keys move along it.
* At phone width the chart fits the screen and the pane is the full-screen overlay.
"""

from __future__ import annotations

import json
import mimetypes
import re
import tempfile
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from jinja2 import Environment, FileSystemLoader

from spark.charts.artefact import Chart, Diagram, csv_bytes, payload
from spark.charts.theme import theme_named
from spark.web.endpoints.preview import FRAME_POLICY
from tests.test_charts_definition import GOOD, make
from tests.test_preview_renderers import BROWSER, stop_browser

WEB = Path(__file__).resolve().parents[1] / "src" / "spark" / "web"
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"
RUN_BUDGET_S = 300

pytestmark = [
    pytest.mark.skipif(BROWSER is None, reason="no Chromium is installed"),
    pytest.mark.timeout(2 * RUN_BUDGET_S + 60),
]

TITLES = {
    "bar": "Units sold by quarter",
    "line": "Sales by region",
    "area": "Stacked units",
    "scatter": "Size against cost",
    "pie": "Share by region",
    "heatmap": "Calls by hour",
    "treemap": "Space by group",
    "sankey": "Where visitors go",
    "graph": "Who talks to whom",
    "radar": "Skills compared",
    "funnel": "Sign-up funnel",
    "boxplot": "Days to close",
}
DIAGRAM = (
    "flowchart TD\n"
    '  A["<img src=x onerror=window.__pwned=1>Start"] --> B{Gate green?}\n'
    "  B -->|yes| C[Stamp]\n  B -->|no| D[Fix]\n  D --> A"
)


def artefacts() -> dict[int, Chart | Diagram]:
    found: dict[int, Chart | Diagram] = {}
    for number, name in enumerate(sorted(GOOD), 1):
        found[number] = Chart(TITLES[name], f"The {name} chart.", make(name))
    found[len(found) + 1] = Diagram("Release flow", "How a merge reaches a stamp.", DIAGRAM)
    return found


PAGE = """<!DOCTYPE html>
<html lang="en" data-bs-theme="{theme}"><head><meta charset="utf-8"><title>charts</title>
<link href="/static/css/cognisn.css" rel="stylesheet">
<link href="/static/css/base.css" rel="stylesheet">
<link href="/static/css/spark.css" rel="stylesheet">
<link rel="stylesheet" href="/static/css/preview.css">
<link rel="stylesheet" href="/static/css/charts.css">
<script src="/static/vendor/mermaid/mermaid.min.js"></script>
</head><body style="margin:0">
<div style="display:flex;min-height:100vh"><div style="flex:1;min-width:0;padding:8px">
<div id="chat-messages"></div></div>{pane}</div>
<div id="tool-panel" style="display:flex"></div>
<pre id="results">pending</pre>
<script src="/static/vendor/marked/marked.umd.js"></script>
<script src="/static/vendor/dompurify/purify.min.js"></script>
<script src="/static/js/markdown.js"></script>
<script src="/static/js/diagrams.js"></script>
<script src="/static/js/main.js"></script>
<script src="/static/js/preview.js"></script>
<script src="/static/js/charts.js"></script>
<script src="/static/js/chat.js"></script>
<script>
const CARDS = {cards};
const MODE = '{mode}';
const saved = [];
HTMLAnchorElement.prototype.click = function () {{
  saved.push({{ href: this.href, name: this.getAttribute('download') }});
}};
function wait(check, ms) {{
  return new Promise(resolve => {{
    const start = Date.now();
    (function poll() {{
      let value = null;
      try {{ value = check(); }} catch (err) {{ value = null; }}
      if (value || Date.now() - start > ms) return resolve(value);
      setTimeout(poll, 50);
    }})();
  }});
}}
function line(card) {{ return 'Made.\\n[spark-preview] ' + JSON.stringify(card); }}
function history(card, index) {{
  const tool = card.kind === 'chart' ? 'create_chart' : 'create_diagram';
  appendMessage({{ role: 'assistant', content: JSON.stringify([
    {{ type: 'text', text: 'Here it is.' }},
    {{ type: 'tool_use', id: 'u' + index, name: tool, input: {{}} }}]) }});
  appendMessage({{ role: 'user', content: '[TOOL_RESULTS]' + JSON.stringify([
    {{ type: 'tool_result', tool_use_id: 'u' + index, content: line(card) }}]) }});
}}
function report(out) {{
  document.getElementById('results').textContent = JSON.stringify(out);
  return fetch('/report', {{ method: 'POST', body: JSON.stringify(out) }});
}}
async function imageSize(href) {{
  const img = new Image();
  await new Promise((ok, bad) => {{ img.onload = ok; img.onerror = bad; img.src = href; }});
  const canvas = document.createElement('canvas');
  canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0);
  return {{ width: img.naturalWidth, height: img.naturalHeight,
           corner: Array.from(ctx.getImageData(3, 3, 1, 1).data.slice(0, 3)) }};
}}
async function lastSave(count) {{
  await wait(() => saved.length >= count, 30000);
  return saved[count - 1] || null;
}}
(async () => {{
  const out = {{ errors: [] }};
  window.addEventListener('error', e => out.errors.push(String(e.message)));
  try {{
    // History: every card but the last; the last arrives live, as the sender's turn.
    CARDS.slice(0, -1).forEach(history);
    updateStreamingToolComplete('live', 'create_diagram', line(CARDS[CARDS.length - 1]), 'success');
    updateStreamingToolComplete('bad', 'create_chart', 'The chart was not made: no', 'error');
    // A Mermaid block in an ordinary reply still draws (not an artefact).
    appendMessage({{ role: 'assistant', content: '```mermaid\\nflowchart LR\\n  X["<b>bold</b>"] --> Y\\n```' }});
    const figures = () => [...document.querySelectorAll('.chart-card')];
    await wait(() => figures().length === CARDS.length &&
      figures().every(f => f.querySelector('.chart-card-canvas').dataset.drawn), 120000);
    out.cards = figures().map(f => {{
      const canvas = f.querySelector('.chart-card-canvas');
      const chart = window.echarts ? echarts.getInstanceByDom(canvas) : null;
      const option = chart ? chart.getOption() : null;
      return {{
        kind: f.dataset.kind, drawn: canvas.dataset.drawn,
        title: f.querySelector('.chart-card-title').textContent,
        caption: f.querySelector('.chart-card-caption').textContent,
        height: Math.round(canvas.getBoundingClientRect().height),
        width: Math.round(canvas.getBoundingClientRect().width),
        series: option ? option.series.map(s => s.type) : [],
        svg: !!canvas.querySelector('svg'),
        foreignObjects: canvas.querySelectorAll('foreignObject').length,
        toolbar: [...f.querySelectorAll('.chart-action')].map(a => a.dataset.action),
        toolbarRole: f.querySelector('.chart-toolbar').getAttribute('role'),
        toolbarLabel: f.querySelector('.chart-toolbar').getAttribute('aria-label'),
      }};
    }});
    const config = mermaid.mermaidAPI.getConfig();
    out.mermaid = {{ securityLevel: config.securityLevel, htmlLabels: config.htmlLabels,
      flowchartHtmlLabels: config.flowchart && config.flowchart.htmlLabels,
      secure: config.secure }};
    await renderMermaidDiagrams(document.getElementById('chat-messages'));
    const reply = document.querySelector('.mermaid[data-processed]');
    out.replyDiagram = {{ svg: !!(reply && reply.querySelector('svg')),
      foreignObjects: reply ? reply.querySelectorAll('foreignObject').length : -1 }};
    // A theme change sets Mermaid up again (main.js): still the one configuration.
    document.dispatchEvent(new Event('app-theme-changed'));
    const again = mermaid.mermaidAPI.getConfig();
    out.afterThemeChange = {{ securityLevel: again.securityLevel, htmlLabels: again.htmlLabels,
      flowchartHtmlLabels: again.flowchart && again.flowchart.htmlLabels }};
    out.pwned = !!window.__pwned;
    out.cardsCount = document.querySelectorAll('.chart-card').length;

    if (MODE === 'phone') {{
      document.querySelector('.chart-card .chart-action[data-action="open"]').click();
      await wait(() => document.querySelector('.preview-panel[data-rendered]'), 60000);
      const pane = document.getElementById('preview-pane');
      out.phone = {{ overlay: pane.classList.contains('preview-overlay'),
        viewport: window.innerWidth,
        widest: Math.max(...[...document.querySelectorAll('.chart-card')]
          .map(f => Math.round(f.getBoundingClientRect().right))) }};
      await report(out);
      return;
    }}

    const bar = figures().find(f => f.querySelector('.chart-card-title').textContent === 'Units sold by quarter');
    const diagram = figures().find(f => f.dataset.kind === 'diagram');
    const click = (fig, action) => fig.querySelector(`.chart-action[data-action="${{action}}"]`).click();

    // Keyboard: the arrow keys move along the toolbar.
    const first = bar.querySelector('.chart-action');
    first.focus();
    first.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'ArrowRight', bubbles: true }}));
    out.arrow = document.activeElement.dataset.action || document.activeElement.className;

    click(bar, 'png');
    const png = await lastSave(1);
    out.png = {{ name: png && png.name, ...(png ? await imageSize(png.href) : {{}}) }};
    bar.querySelector('.chart-match-input').checked = true;
    click(bar, 'png');
    const themed = await lastSave(2);
    out.pngThemed = themed ? await imageSize(themed.href) : null;
    bar.querySelector('.chart-match-input').checked = false;
    click(bar, 'svg');
    const svg = await lastSave(3);
    const svgText = svg ? await (await fetch(svg.href)).text() : '';
    out.svg = {{ name: svg && svg.name, isSvg: svgText.includes('<svg'),
      titled: svgText.includes('Units sold by quarter') }};
    const csv = bar.querySelector('.chart-action[data-action="csv"]');
    out.csv = {{ href: csv.getAttribute('href'), name: csv.getAttribute('download') }};
    click(diagram, 'svg');
    const dsvg = await lastSave(4);
    const dText = dsvg ? await (await fetch(dsvg.href)).text() : '';
    out.diagramSvg = {{ name: dsvg && dsvg.name, titled: dText.includes('Release flow'),
      foreignObjects: (dText.match(/foreignObject/g) || []).length }};
    click(diagram, 'png');
    const dpng = await lastSave(5);
    out.diagramPng = dpng ? {{ name: dpng.name, ...(await imageSize(dpng.href)) }} : null;

    // Open in preview: the chart, then the diagram, in the sandboxed frame.
    click(bar, 'open');
    const chartPanel = await wait(() => document.querySelector('.preview-panel[data-rendered]'), 60000);
    out.paneChart = chartPanel ? {{ rendered: chartPanel.dataset.rendered,
      detail: JSON.parse(chartPanel.dataset.detail || '{{}}'),
      toolbar: [...chartPanel.querySelectorAll('.chart-action')].map(a => a.dataset.action) }} : null;
    click(diagram, 'open');
    const diagramPanel = await wait(() => [...document.querySelectorAll('.preview-panel')]
      .find(p => !p.hidden && p.dataset.rendered), 60000);
    out.paneDiagram = diagramPanel ? {{ rendered: diagramPanel.dataset.rendered,
      detail: JSON.parse(diagramPanel.dataset.detail || '{{}}') }} : null;
    out.pwnedAfter = !!window.__pwned;
  }} catch (err) {{
    out.errors.push(String(err && err.stack || err));
  }}
  await report(out);
}})();
</script></body></html>"""


class Harness:
    def __init__(self, theme: str, mode: str) -> None:
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
        frame = env.get_template("preview_frame.html").render().encode("utf-8")
        pane = env.get_template("_preview_pane.html").render(conversation={"id": 1})
        found = artefacts()
        cards = [
            {
                "file_id": fid,
                "conversation_id": 1,
                "name": f"a{fid}.json",
                "title": item.title,
                "type": "application/json",
                "kind": "chart" if isinstance(item, Chart) else "diagram",
                "size": 10,
            }
            for fid, item in found.items()
        ]
        page = PAGE.format(theme=theme, pane=pane, cards=json.dumps(cards), mode=mode).encode()
        self.reported = threading.Event()
        self.report: dict[str, Any] = {}
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: bytes, kind: str, **headers: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name.replace("_", "-"), value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/report":
                    harness.report = json.loads(body)
                    harness.reported.set()
                self._send(204, b"", "text/plain")

            def do_GET(self) -> None:  # noqa: N802
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/harness":
                    self._send(200, page, "text/html; charset=utf-8")
                elif url.path == "/preview/frame":
                    self._send(
                        200, frame, "text/html; charset=utf-8", Content_Security_Policy=FRAME_POLICY
                    )
                elif match := re.fullmatch(r"/charts/api/(\d+)(/data\.csv)?", url.path):
                    item = found.get(int(match.group(1)))
                    if item is None:
                        self._send(404, b"{}", "application/json")
                    elif match.group(2):
                        assert isinstance(item, Chart)
                        self._send(200, csv_bytes(item), "text/csv")
                    else:
                        body = payload(
                            item,
                            theme_named(query.get("theme", ["light"])[0]),
                            export=query.get("export") == ["1"],
                        )
                        self._send(200, json.dumps(body).encode(), "application/json")
                elif url.path.startswith("/static/"):
                    target = (STATIC / url.path[len("/static/") :]).resolve()
                    if not target.is_file() or STATIC.resolve() not in target.parents:
                        self._send(404, b"", "text/plain")
                        return
                    kind = (
                        "text/javascript"
                        if target.suffix in (".js", ".mjs")
                        else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    )
                    self._send(200, target.read_bytes(), kind)
                else:
                    self._send(404, b"", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_page(theme: str, mode: str, size: str) -> dict[str, Any]:
    import subprocess

    assert BROWSER is not None
    harness = Harness(theme, mode)
    try:
        with tempfile.TemporaryDirectory(
            prefix="spark-charts-", ignore_cleanup_errors=True
        ) as profile:
            browser = subprocess.Popen(
                [
                    BROWSER,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--window-size={size}",
                    f"--user-data-dir={profile}",
                    f"http://127.0.0.1:{harness.port}/harness",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                finished = harness.reported.wait(RUN_BUDGET_S)
            finally:
                stop_browser(browser)
        assert finished, f"the chart harness did not report in {RUN_BUDGET_S} s"
        return harness.report
    finally:
        harness.close()


@pytest.fixture(scope="module")
def desktop() -> Iterator[dict[str, Any]]:
    yield run_page("dark", "desktop", "1400,1000")


@pytest.fixture(scope="module")
def phone() -> Iterator[dict[str, Any]]:
    yield run_page("light", "phone", "390,844")


def test_no_page_errors(desktop: dict[str, Any]) -> None:
    assert desktop["errors"] == []


def test_every_card_draws_from_the_chart_api(desktop: dict[str, Any]) -> None:
    cards = desktop["cards"]
    assert len(cards) == 13 and desktop["cardsCount"] == 13
    expected = {
        "Units sold by quarter": "bar",
        "Sales by region": "line",
        "Stacked units": "line",
        "Size against cost": "scatter",
        "Share by region": "pie",
        "Calls by hour": "heatmap",
        "Space by group": "treemap",
        "Where visitors go": "sankey",
        "Who talks to whom": "graph",
        "Skills compared": "radar",
        "Sign-up funnel": "funnel",
        "Days to close": "boxplot",
    }
    for card in cards:
        assert card["height"] == 320, card
        if card["kind"] == "chart":
            assert card["drawn"] == "chart", card
            assert card["series"][0] == expected[card["title"]], card
            assert card["toolbar"] == ["open", "png", "svg", "csv"]
            assert card["caption"].startswith("The ")
        else:
            assert (card["drawn"], card["svg"], card["foreignObjects"]) == ("diagram", True, 0)
            assert card["toolbar"] == ["open", "png", "svg"]
        assert card["toolbarRole"] == "toolbar" and card["title"] in card["toolbarLabel"]


def test_mermaid_is_strict_with_html_labels_off_everywhere(desktop: dict[str, Any]) -> None:
    assert desktop["mermaid"]["securityLevel"] == "strict"
    assert desktop["mermaid"]["htmlLabels"] is False
    assert desktop["mermaid"]["flowchartHtmlLabels"] is False
    assert {"securityLevel", "htmlLabels", "flowchart"} <= set(desktop["mermaid"]["secure"])
    assert desktop["replyDiagram"] == {"svg": True, "foreignObjects": 0}
    assert desktop["afterThemeChange"] == {
        "securityLevel": "strict",
        "htmlLabels": False,
        "flowchartHtmlLabels": False,
    }
    assert desktop["pwned"] is False and desktop["pwnedAfter"] is False


def test_the_arrow_keys_move_along_the_toolbar(desktop: dict[str, Any]) -> None:
    assert desktop["arrow"] == "png"


def test_png_is_twice_the_size_and_on_white_unless_matching_the_theme(
    desktop: dict[str, Any],
) -> None:
    png = desktop["png"]
    assert png["name"] == "Units sold by quarter.png"
    assert (png["width"], png["height"]) == (2400, 1440)
    assert png["corner"] == [255, 255, 255]
    assert desktop["pngThemed"]["corner"] == [12, 20, 40]  # the dark theme's background


def test_svg_and_csv(desktop: dict[str, Any]) -> None:
    assert desktop["svg"] == {"name": "Units sold by quarter.svg", "isSvg": True, "titled": True}
    assert desktop["csv"]["href"].endswith("/charts/api/2/data.csv")
    assert desktop["csv"]["name"] == "Units sold by quarter.csv"


def test_a_diagram_saves_as_svg_and_png_with_its_title(desktop: dict[str, Any]) -> None:
    assert desktop["diagramSvg"] == {
        "name": "Release flow.svg",
        "titled": True,
        "foreignObjects": 0,
    }
    image = desktop["diagramPng"]
    assert image["name"] == "Release flow.png" and image["width"] > 200
    assert image["corner"] == [255, 255, 255]


def test_open_in_preview_draws_in_the_sandboxed_frame(desktop: dict[str, Any]) -> None:
    assert desktop["paneChart"]["rendered"] == "chart"
    assert desktop["paneChart"]["detail"]["type"] == "bar"
    assert desktop["paneChart"]["toolbar"] == ["png", "svg", "csv"]
    assert desktop["paneDiagram"]["rendered"] == "diagram"
    assert desktop["paneDiagram"]["detail"] == {"svg": True, "foreignObjects": 0}


def test_phone_width(phone: dict[str, Any]) -> None:
    assert phone["errors"] == []
    assert phone["phone"]["overlay"] is True
    assert phone["phone"]["widest"] <= phone["phone"]["viewport"]
    assert all(card["height"] == 260 for card in phone["cards"])
