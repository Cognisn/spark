"""create_chart and create_diagram through the tool registry, on SQLite and PostgreSQL.

* A chart is stored in the sender's area as a file of the conversation (kind
  ``chart``, its title, a snapshot of only the columns it uses), and the result names
  the title and file id and carries the card line the chat page draws.
* Data can come from a CSV or Excel file: one of this conversation's (any member's)
  or one of the sender's own. Another user's file elsewhere answers exactly as a file
  that does not exist, and nothing is stored.
* A definition SPARK cannot draw, a missing title and a full quota are refused with a
  plain reason, and nothing is stored.
* create_diagram keeps the Mermaid source with its title; a directive is refused.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark import files as files_pkg
from spark import mode
from spark.charts.artefact import Chart, Diagram, read_artefact
from spark.database import conversation_members, conversations
from spark.database import files as files_db
from spark.database.scope import Role
from spark.identity.permissions import USER, role_permissions
from spark.mode import Mode
from spark.preview.service import parse_card
from spark.tools.registry import PERMISSIONS_KEY, execute_builtin_tool, get_builtin_tools

ADA = "user-ada-0001"
CORA = "user-cora-0002"
SAM = "user-sam-0004"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[_Settings]:
    values = _Settings({"files.root": str(tmp_path / "volume"), "files.quota_mb": 0})
    files_pkg.configure(values, data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
    try:
        yield values
    finally:
        files_pkg.reset()


@pytest.fixture
def conversation(bridge: Any, settings: _Settings) -> int:
    cid: int = bridge.call(conversations.create_conversation, "Talk", "stub/model", ADA)
    bridge.call(conversation_members.share, ADA, cid, CORA, Role.CONTRIBUTOR)
    return cid


def config(bridge: Any, user: str = ADA, cid: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "embedded_tools": {},
        "_user_guid": user,
        "_files_db": bridge,
        PERMISSIONS_KEY: role_permissions(USER),
    }
    if cid is not None:
        out["_kg_conversation_id"] = cid
    return out


SALES = {
    "type": "bar",
    "data": {
        "columns": ["Quarter", "Sales", "Notes"],
        "rows": [["Q1", 10, "x"], ["Q2", 14, "y"]],
    },
    "encoding": {"x": "Quarter", "y": "Sales"},
}


def run(name: str, args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, bool]:
    return execute_builtin_tool(name, args, cfg)


def stored(bridge: Any, user: str, file_id: int) -> tuple[Any, Chart | Diagram]:
    row = bridge.call(files_db.get_user_file, user, file_id)
    assert row is not None
    with files_pkg.file_store().open(user, str(row["storage_key"])) as handle:
        return row, read_artefact(handle.read())


def upload(bridge: Any, user: str, name: str, data: bytes, mime: str, cid: int | None) -> int:
    area = files_pkg.file_area(bridge)
    return area.save(
        user, f"uploads/{name}", data, filename=name, mime_type=mime, conversation_id=cid
    )


def xlsx(rows: list[list[Any]], sheet: str) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = "First"  # type: ignore[union-attr]
    target = book.create_sheet(sheet)
    for row in rows:
        target.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


class TestCreateChart:
    def test_offered_in_the_documents_category(self, settings: _Settings) -> None:
        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert {"create_chart", "create_diagram"} <= names
        from spark.tools.registry import tool_category

        assert tool_category("create_chart") == tool_category("create_diagram") == "documents"

    def test_stored_with_title_kind_snapshot_and_card(self, bridge: Any, conversation: int) -> None:
        text, error = run(
            "create_chart",
            {"title": "Sales <by> quarter", "definition": SALES, "caption": "Q1 and Q2"},
            config(bridge, cid=conversation),
        )
        assert error is False, text
        card = parse_card(text)
        assert card is not None
        assert (card.kind, card.conversation_id, card.title) == (
            "chart",
            conversation,
            "Sales ＜by＞ quarter",
        )
        assert f"file {card.file_id}" in text.splitlines()[0]
        row, chart = stored(bridge, ADA, card.file_id)
        assert (row["kind"], row["title"], row["conversation_id"]) == (
            "chart",
            card.title,
            conversation,
        )
        assert isinstance(chart, Chart) and chart.caption == "Q1 and Q2"
        assert chart.prepared.table.columns == ("Quarter", "Sales")  # Notes is not kept

    def test_a_definition_it_cannot_draw_is_refused_and_nothing_stored(
        self, bridge: Any, conversation: int
    ) -> None:
        bad = {**SALES, "type": "gauge"}
        text, error = run(
            "create_chart", {"title": "T", "definition": bad}, config(bridge, cid=conversation)
        )
        assert error is True and text.startswith("The chart was not made: type must be one of")
        assert bridge.call(files_db.list_user_files, ADA) == []

    def test_a_title_is_required(self, bridge: Any, conversation: int) -> None:
        text, error = run("create_chart", {"definition": SALES}, config(bridge, cid=conversation))
        assert error is True and "title is required" in text
        assert bridge.call(files_db.list_user_files, ADA) == []

    def test_quota_refusal_stores_nothing(
        self, bridge: Any, conversation: int, settings: _Settings
    ) -> None:
        settings.values["files.quota_mb"] = 0.0001  # about 100 bytes
        text, error = run(
            "create_chart", {"title": "T", "definition": SALES}, config(bridge, cid=conversation)
        )
        assert error is True and text.startswith("The chart was not saved: Storage quota")
        assert bridge.call(files_db.list_user_files, ADA) == []

    def test_csv_from_another_member_of_the_conversation(
        self, bridge: Any, conversation: int
    ) -> None:
        fid = upload(
            bridge, CORA, "sales.csv", b"Quarter;Sales\nQ1;3\nQ2;4\n", "text/csv", conversation
        )
        definition = {
            "type": "pie",
            "data": {"file_id": fid},
            "encoding": {"x": "Quarter", "y": "Sales"},
        }
        text, error = run(
            "create_chart",
            {"title": "From CSV", "definition": definition},
            config(bridge, cid=conversation),
        )
        assert error is False, text
        card = parse_card(text)
        assert card is not None
        _, chart = stored(bridge, ADA, card.file_id)
        assert isinstance(chart, Chart)
        assert chart.prepared.table.rows == (("Q1", "3"), ("Q2", "4"))
        assert chart.source == {"file_id": fid, "sheet": None}

    def test_excel_sheet_and_columns(self, bridge: Any, conversation: int) -> None:
        data = xlsx([["Region", "Total", "Extra"], ["N", 5, 1], ["S", 7, 2]], "Sales")
        fid = upload(
            bridge,
            ADA,
            "book.xlsx",
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            None,
        )
        definition = {
            "type": "bar",
            "data": {"file_id": fid, "sheet": "Sales", "columns": ["Region", "Total"]},
            "encoding": {"x": "Region", "y": "Total"},
        }
        text, error = run(
            "create_chart",
            {"title": "Book", "definition": definition},
            config(bridge, cid=conversation),
        )
        assert error is False, text
        missing = {**definition, "data": {"file_id": fid, "sheet": "Nope"}}
        text, error = run(
            "create_chart",
            {"title": "Book", "definition": missing},
            config(bridge, cid=conversation),
        )
        assert error is True and "no sheet Nope; its sheets are: First, Sales" in text

    def test_another_users_file_is_not_found(self, bridge: Any, conversation: int) -> None:
        elsewhere = upload(bridge, SAM, "secret.csv", b"a,b\nx,1\n", "text/csv", None)
        definition = {
            "type": "pie",
            "data": {"file_id": elsewhere},
            "encoding": {"x": "a", "y": "b"},
        }
        text, error = run(
            "create_chart",
            {"title": "T", "definition": definition},
            config(bridge, cid=conversation),
        )
        assert error is True
        assert f"File {elsewhere} is not one of this conversation's files or yours." in text
        missing = {**definition, "data": {"file_id": elsewhere + 1000}}
        other, _ = run(
            "create_chart", {"title": "T", "definition": missing}, config(bridge, cid=conversation)
        )
        assert other.replace(str(elsewhere + 1000), "N") == text.replace(str(elsewhere), "N")
        assert bridge.call(files_db.list_user_files, ADA) == []

    def test_a_file_of_another_conversation_of_someone_else_is_not_found(
        self, bridge: Any, conversation: int
    ) -> None:
        theirs: int = bridge.call(conversations.create_conversation, "Theirs", "stub/model", CORA)
        fid = upload(bridge, CORA, "c.csv", b"a,b\nx,1\n", "text/csv", theirs)
        definition = {"type": "pie", "data": {"file_id": fid}, "encoding": {"x": "a", "y": "b"}}
        text, error = run(
            "create_chart",
            {"title": "T", "definition": definition},
            config(bridge, cid=conversation),
        )
        assert error is True and "is not one of this conversation's files or yours" in text

    def test_not_a_spreadsheet(self, bridge: Any, conversation: int) -> None:
        fid = upload(bridge, ADA, "a.pdf", b"%PDF-1.4", "application/pdf", conversation)
        definition = {"type": "pie", "data": {"file_id": fid}, "encoding": {"x": "a", "y": "b"}}
        text, error = run(
            "create_chart",
            {"title": "T", "definition": definition},
            config(bridge, cid=conversation),
        )
        assert error is True and "not a CSV or Excel file" in text

    def test_without_a_signed_in_user(self, settings: _Settings) -> None:
        text, error = run(
            "create_chart", {"title": "T", "definition": SALES}, {"embedded_tools": {}}
        )
        assert error is True and "signed-in user" in text


class TestCreateDiagram:
    def test_stored_with_its_source_and_card(self, bridge: Any, conversation: int) -> None:
        source = "flowchart TD\n  A[Start] --> B{Ready?}\n  B -->|yes| C[Ship]"
        text, error = run(
            "create_diagram",
            {"title": "Release flow", "source": source, "caption": "How we ship"},
            config(bridge, cid=conversation),
        )
        assert error is False, text
        card = parse_card(text)
        assert card is not None and card.kind == "diagram"
        row, diagram = stored(bridge, ADA, card.file_id)
        assert (row["kind"], row["title"]) == ("diagram", "Release flow")
        assert isinstance(diagram, Diagram) and diagram.source == source

    def test_a_directive_is_refused(self, bridge: Any, conversation: int) -> None:
        source = "%%{init: {'securityLevel': 'loose'}}%%\nflowchart TD\nA-->B"
        text, error = run(
            "create_diagram", {"title": "T", "source": source}, config(bridge, cid=conversation)
        )
        assert error is True and "settings" in text
        assert bridge.call(files_db.list_user_files, ADA) == []

    def test_a_title_is_required(self, bridge: Any, conversation: int) -> None:
        text, error = run(
            "create_diagram",
            {"title": " ", "source": "graph TD\nA-->B"},
            config(bridge, cid=conversation),
        )
        assert error is True and "title is required" in text

    def test_outside_a_conversation_there_is_no_card(
        self, bridge: Any, settings: _Settings
    ) -> None:
        text, error = run(
            "create_diagram", {"title": "T", "source": "graph TD\nA-->B"}, config(bridge)
        )
        assert error is False and parse_card(text) is None
        assert "file " in text


def test_tool_definitions_require_titles() -> None:
    from spark.tools import charts, diagrams

    for tool in charts.get_tools() + diagrams.get_tools():
        assert "title" in tool["inputSchema"]["required"]
        json.dumps(tool)
    assert mode is not None
