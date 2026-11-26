"""Charts and diagrams in server mode on the live stack (wave 32, kick-off scope 2; PC5,
PC19, PC20).

The model stub calls ``create_chart`` and ``create_diagram`` in real turns, so each
definition goes through SPARK's tool, validator, builder and file area as a model's
would. Every chart type is drawn from inline data, one chart from a CSV file and one
from an Excel file attached to the conversation, and one Mermaid diagram. A member of
the shared conversation opens them; a stranger cannot. A real headless Chromium,
signed in with the owner's session, opens the conversation in the light and dark
themes, opens a chart in the preview pane, and saves PNG, SVG and CSV files through
the toolbar, which are then checked as files. Users and data are synthetic.
"""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.live.conftest import Person, Stack, invite_and_accept
from tests.live.live_helpers import (
    can_read,
    csv_bytes,
    new_conversation,
    ok,
    preview_card,
    tool_turn,
    upload,
    xlsx_bytes,
)
from tests.test_charts_definition import GOOD

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900)]

#: The chart types of PC19, in the schema's order.
TYPES = (
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
DIAGRAM = "flowchart TD\n  A[Sow] --> B{Frost over?}\n  B -->|yes| C[Plant out]\n  B -->|no| A"
FILE_COLUMNS = ["Month", "Visits"]
FILE_ROWS = [["Jan", 120], ["Feb", 135], ["Mar", 160], ["Apr", 150]]


@dataclass
class Drawn:
    owner: Person
    reader: Person
    stranger: Person
    conversation: int
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)


@pytest.fixture(scope="module")
def drawn(stack: Stack, admin: Person, configured: None, model_id: str) -> Iterator[Drawn]:
    """A conversation of Chloe's with a chart of every type, two from files and a
    diagram, shared read-only with Rhys; Sam is a stranger."""
    people = [
        invite_and_accept(stack, admin, f"{who}-{stack.run}@example.com", "user")
        for who in ("chloe", "rhys", "sam")
    ]
    owner, reader, stranger = people
    conv = new_conversation(owner, model_id, f"Charts {stack.run}")
    found = Drawn(owner, reader, stranger, conv)
    for kind in TYPES:
        _e, _first, result = tool_turn(
            stack,
            owner,
            conv,
            f"Draw a {kind} chart.",
            "create_chart",
            {"title": f"{kind.capitalize()} of the garden", "definition": GOOD[kind]},
        )
        found.cards[kind] = preview_card(result)

    csv_file = upload(
        owner.browser, "visits.csv", csv_bytes(FILE_COLUMNS, FILE_ROWS), "text/csv", conv
    )
    xlsx_file = upload(
        owner.browser,
        "visits.xlsx",
        xlsx_bytes("Visits", FILE_COLUMNS, FILE_ROWS),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        conv,
    )
    for key, data in (
        ("csv", {"file_id": csv_file}),
        ("xlsx", {"file_id": xlsx_file, "sheet": "Visits", "columns": FILE_COLUMNS}),
    ):
        definition = {"type": "line", "data": data, "encoding": {"x": "Month", "y": "Visits"}}
        _e, _first, result = tool_turn(
            stack,
            owner,
            conv,
            f"Chart the {key} file.",
            "create_chart",
            {"title": f"Visits from the {key} file", "definition": definition},
        )
        found.cards[key] = preview_card(result)

    _e, _first, result = tool_turn(
        stack,
        owner,
        conv,
        "Draw the sowing flow.",
        "create_diagram",
        {"title": "Sowing flow", "source": DIAGRAM, "caption": "When to plant out."},
    )
    found.cards["diagram"] = preview_card(result)
    ok(
        owner.browser.post(
            f"/conversations/api/{conv}/members",
            json={"user_id": reader.warden_id, "role": "viewer"},
        )
    )
    yield found
    for person in people:
        person.browser.close()


def _view(person: Person, file_id: int, theme: str = "light") -> dict[str, Any]:
    found: dict[str, Any] = ok(
        person.browser.get(f"/charts/api/{file_id}", params={"theme": theme})
    )
    return found


def test_every_chart_type_is_drawn_from_inline_data_in_both_themes(
    stack: Stack, drawn: Drawn
) -> None:
    """PC19: each of the twelve types is validated, stored as a chart artefact with its
    title and built into an option for the light and the dark theme."""
    for kind in TYPES:
        card = drawn.cards[kind]
        assert card["kind"] == "chart" and card["conversation_id"] == drawn.conversation, card
        row = stack.sql(
            "SELECT kind, title, user_guid, conversation_id FROM user_files WHERE id = %s",
            (card["file_id"],),
        )
        assert row == [
            ("chart", f"{kind.capitalize()} of the garden", drawn.owner.guid, drawn.conversation)
        ], row
        light, dark = (_view(drawn.owner, card["file_id"], t) for t in ("light", "dark"))
        assert light["type"] == kind and light["kind"] == "chart", light
        assert light["option"]["series"], (kind, light["option"])
        assert (light["theme"], dark["theme"]) == ("light", "dark"), (light["theme"], dark["theme"])
        assert light["option"] != dark["option"], f"{kind}: the themes draw the same"
        assert "function" not in str(light["option"]), f"{kind}: code in the option"


def test_a_chart_reads_its_data_from_a_csv_and_from_an_excel_file(drawn: Drawn) -> None:
    """PC19: data from a CSV file and from a sheet of an Excel file in the conversation;
    the chart keeps a snapshot of the rows it used."""
    for key in ("csv", "xlsx"):
        view = _view(drawn.owner, drawn.cards[key]["file_id"])
        assert view["type"] == "line", view
        assert view["rows"] == len(FILE_ROWS), view["rows"]
        data = drawn.owner.browser.get(f"/charts/api/{drawn.cards[key]['file_id']}/data.csv")
        assert data.status_code == 200, data.text
        assert _csv(data.text) == [FILE_COLUMNS, *[[m, str(v)] for m, v in FILE_ROWS]]


def test_a_diagram_is_stored_with_its_title_and_served_as_its_source(
    stack: Stack, drawn: Drawn
) -> None:
    """PC20: create_diagram keeps the Mermaid source as a diagram artefact."""
    card = drawn.cards["diagram"]
    assert card["kind"] == "diagram" and card["title"] == "Sowing flow", card
    view = _view(drawn.owner, card["file_id"])
    assert view["kind"] == "diagram" and view["source"] == DIAGRAM, view
    assert view["caption"] == "When to plant out.", view
    assert drawn.owner.browser.get(f"/charts/api/{card['file_id']}/data.csv").status_code == 404


def test_a_member_of_the_shared_conversation_opens_a_chart_and_a_stranger_cannot(
    stack: Stack, drawn: Drawn
) -> None:
    """PC20 with sharing (CS28): a Read-only member opens every chart, the diagram and
    the data, and sees them in the panel; a stranger is answered as if none existed;
    the data download is audited."""
    assert can_read(drawn.reader, drawn.conversation)
    for card in drawn.cards.values():
        view = _view(drawn.reader, card["file_id"])
        assert view["file_id"] == card["file_id"], view
        assert drawn.stranger.browser.get(f"/charts/api/{card['file_id']}").status_code == 404
    file_id = drawn.cards["bar"]["file_id"]
    since = time.time()
    data = drawn.reader.browser.get(f"/charts/api/{file_id}/data.csv")
    assert data.status_code == 200 and _csv(data.text)[0][0] == "Quarter", data.text[:200]
    assert drawn.stranger.browser.get(f"/charts/api/{file_id}/data.csv").status_code == 404
    audited = stack.sql(
        "SELECT count(*) FROM warden.audit_events WHERE action = 'spark.file.downloaded' "
        "AND resource_id = %s AND occurred_at >= to_timestamp(%s)",
        (str(file_id), since - 5),
    )
    assert audited[0][0] >= 1, audited
    panel = ok(drawn.reader.browser.get(f"/artefacts/api/{drawn.conversation}"))
    listed = {a["file_id"] for a in panel["generated"]}
    assert {c["file_id"] for c in drawn.cards.values()} <= listed, panel["generated"]
    assert panel["can_rename"] is False


def test_a_definition_the_validator_refuses_is_explained_to_the_model(
    stack: Stack, drawn: Drawn
) -> None:
    """PC19: a chart SPARK cannot draw is refused with what to change, the refusal is
    what the model sees, and nothing is stored."""
    before = stack.sql(
        "SELECT count(*) FROM user_files WHERE user_guid = %s AND kind = 'chart'",
        (drawn.owner.guid,),
    )[0][0]
    bad = {
        "type": "pie",
        "data": {"columns": ["Region", "Sales"], "rows": [["North", 5], ["South", -2]]},
        "encoding": {"x": "Region", "y": "Sales"},
    }
    _e, _first, result = tool_turn(
        stack,
        drawn.owner,
        drawn.conversation,
        "Draw a pie with a negative slice.",
        "create_chart",
        {"title": "Impossible pie", "definition": bad},
    )
    assert "The chart was not made" in result, result[:2000]
    assert "[spark-preview]" not in result
    after = stack.sql(
        "SELECT count(*) FROM user_files WHERE user_guid = %s AND kind = 'chart'",
        (drawn.owner.guid,),
    )[0][0]
    assert after == before, "a refused chart was stored"


# -- in a real browser ----------------------------------------------------------------------


def _browser_dir(stack: Stack, name: str) -> Path:
    return stack.sandbox / "browser" / stack.run / name


def test_charts_draw_in_the_message_and_the_pane_and_save_as_files(
    stack: Stack, drawn: Drawn
) -> None:
    """PC20 in Chromium: every card draws in the conversation and in the preview pane in
    both themes; Save as PNG gives a 2x image on white, or on the dark background with
    Match theme; Save as SVG carries the title; Download data saves the CSV."""
    from PIL import Image

    from tests.live.cdp import DESKTOP, chromium_session, click, saved, shim_chat_page, sign_in

    port = int(stack.state["ports"]["web"]) + 60
    url = f"{stack.base_url}/chat/{drawn.conversation}"
    shots = _browser_dir(stack, "shots")
    for theme in ("light", "dark"):
        downloads = _browser_dir(stack, f"downloads-{theme}")
        with chromium_session(port, downloads) as chrome:
            page = chrome.page()
            page.size(*DESKTOP)
            sign_in(page, drawn.owner.browser, theme)
            shim_chat_page(page)  # Skuld question 94, decision 260
            page.navigate(url)
            page.wait(
                f"document.documentElement.getAttribute('data-bs-theme') === '{theme}'",
                30,
                f"the {theme} theme",
            )
            count = len(drawn.cards)
            page.wait(
                f"document.querySelectorAll('.chart-card canvas, .chart-card svg').length >= {count}",
                180,
                f"{count} cards drawn",
            )
            failed = page.evaluate(
                "[...document.querySelectorAll('.chart-card-error')]"
                ".filter(e => !e.hidden).map(e => e.textContent)"
            )
            assert not failed, failed
            page.screenshot(shots / f"chat-charts-{theme}.png")

            bar = drawn.cards["bar"]
            toolbar = f"[aria-label='Chart actions for {bar['title']}']"
            click(page, f"{toolbar} [data-action='open']")
            page.wait(
                "!!document.querySelector('#preview-pane:not([hidden]) iframe')",
                60,
                "the preview pane open",
            )
            time.sleep(2)
            page.screenshot(shots / f"chat-chart-pane-{theme}.png")

            if theme == "dark":
                click(page, f"{toolbar} .chart-match-input")
            click(page, f"{toolbar} [data-action='png']")
            png = saved(downloads, f"{_file_name(bar['title'])}.png")
            click(page, f"{toolbar} [data-action='svg']")
            svg = saved(downloads, f"{_file_name(bar['title'])}.svg")
            click(page, f"{toolbar} [data-action='csv']")
            data = saved(downloads, f"{_file_name(bar['title'])}.csv")
            assert not page.errors(), page.errors()

        with Image.open(png) as image:
            assert image.size == (2400, 1440), image.size
            pixel = image.convert("RGB").getpixel((2, 2))
        assert isinstance(pixel, tuple), pixel
        corner = tuple(int(c) for c in pixel)
        if theme == "light":
            assert corner == (255, 255, 255), f"not on white: {corner}"
        else:
            assert sum(corner) < 200, f"Match theme in dark saved on {corner}"
        text = svg.read_text("utf-8")
        assert text.lstrip().startswith("<svg") or "<svg" in text[:500], text[:200]
        assert bar["title"] in text, "the SVG has no title"
        assert _csv(data.read_text("utf-8"))[0][0] == "Quarter", data.read_text("utf-8")[:200]


def _csv(text: str) -> list[list[str]]:
    """A downloaded CSV's rows (SPARK writes a byte order mark for spreadsheets)."""
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def _file_name(title: str) -> str:
    """The toolbar's file name for a title (charts.js fileName)."""
    import re

    base = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|＜＞]+', " ", title)
    base = re.sub(r"\s+", " ", base).strip()
    base = re.sub(r"^[.\s]+|[.\s]+$", "", base)[:100].strip()
    return base or "chart"
