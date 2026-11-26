"""The chart API (spark.web.endpoints.charts), on SQLite and PostgreSQL.

* ``/charts/api/<file>``: a chart's option for the asked theme (built by SPARK), or a
  diagram's source, for anyone the conversation's access decision lets read it (the
  owner, a contributor, a viewer); 404 for a stranger and a removed member, and for a
  file that is not a chart or a diagram; a damaged chart answers 422 and is not drawn.
* ``/charts/api/<file>/data.csv``: the data snapshot, no formula cells, named from the
  title.

Users are switched as the personal-mode tests switch them (``app.state.user_guid``).
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversation_members
from spark.database import conversations as convdb
from spark.database.scope import Role
from spark.mode import Mode
from spark.preview.service import parse_card
from spark.tools.registry import execute_builtin_tool
from spark.web.server import attach_database, create_app
from tests.web_helpers import sign_in

ADA = "user-ada-0001"
CORA = "user-cora-0002"
VIC = "user-vic-0003"
SAM = "user-sam-0004"

SALES = {
    "type": "bar",
    "data": {"columns": ["Quarter", "Sales"], "rows": [["Q1", 10], ["=cmd", 14]]},
    "encoding": {"x": "Quarter", "y": "Sales"},
}


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture
def client(spark_db_settings: Any, tmp_path: Path) -> Iterator[TestClient]:
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "volume")}),
        data_dir=tmp_path,
        runtime_mode=Mode.PERSONAL,
    )
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    attach_database(app, spark_db_settings, True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    try:
        with TestClient(app, backend_options=options) as client:
            app.state.user_guid = ADA
            sign_in(client)
            yield client
    finally:
        files_pkg.reset()


def _as(client: TestClient, user: str) -> None:
    client.app.state.user_guid = user  # type: ignore[attr-defined]


def _db(client: TestClient) -> Any:
    return client.app.state.sync_db  # type: ignore[attr-defined]


@pytest.fixture
def shared(client: TestClient) -> int:
    db = _db(client)
    cid: int = db.call(convdb.create_conversation, "Shared", "stub/model", ADA)
    db.call(conversation_members.share, ADA, cid, CORA, Role.CONTRIBUTOR)
    db.call(conversation_members.share, ADA, cid, VIC, Role.VIEWER)
    return cid


def _make(client: TestClient, tool: str, args: dict[str, Any], user: str, cid: int) -> int:
    config = {
        "embedded_tools": {},
        "_user_guid": user,
        "_files_db": _db(client),
        "_kg_conversation_id": cid,
    }
    text, error = execute_builtin_tool(tool, args, config)
    assert error is False, text
    card = parse_card(text)
    assert card is not None
    return card.file_id


def test_every_member_gets_the_option_and_nobody_else(client: TestClient, shared: int) -> None:
    fid = _make(client, "create_chart", {"title": "Sales", "definition": SALES}, CORA, shared)
    for who in (ADA, CORA, VIC):
        _as(client, who)
        got = client.get(f"/charts/api/{fid}?theme=dark")
        assert got.status_code == 200, who
        body = got.json()
        assert (body["kind"], body["title"], body["type"], body["theme"]) == (
            "chart",
            "Sales",
            "bar",
            "dark",
        )
        assert body["option"]["series"][0]["data"] == [10, 14]
        assert body["option"]["backgroundColor"] == "transparent"
        assert got.headers["cache-control"] == "private, no-store"
    _as(client, SAM)
    assert client.get(f"/charts/api/{fid}").status_code == 404
    assert client.get(f"/charts/api/{fid}/data.csv").status_code == 404
    _as(client, ADA)
    _db(client).call(conversation_members.remove_member, ADA, shared, VIC)
    _as(client, VIC)
    assert client.get(f"/charts/api/{fid}").status_code == 404


def test_an_export_carries_the_title_on_white(client: TestClient, shared: int) -> None:
    fid = _make(
        client,
        "create_chart",
        {"title": "Sales", "definition": SALES, "caption": "Two quarters"},
        ADA,
        shared,
    )
    body = client.get(f"/charts/api/{fid}?export=1").json()
    assert body["option"]["title"]["text"] == "Sales"
    assert body["option"]["backgroundColor"] == body["background"] == "#ffffff"
    assert body["caption"] == "Two quarters"


def test_a_diagram_answers_its_source(client: TestClient, shared: int) -> None:
    fid = _make(
        client, "create_diagram", {"title": "Flow", "source": "graph TD\nA-->B"}, ADA, shared
    )
    _as(client, VIC)
    body = client.get(f"/charts/api/{fid}").json()
    assert (body["kind"], body["source"]) == ("diagram", "graph TD\nA-->B")
    assert client.get(f"/charts/api/{fid}/data.csv").status_code == 404


def test_csv_of_the_snapshot(client: TestClient, shared: int) -> None:
    fid = _make(
        client, "create_chart", {"title": "Sales by quarter", "definition": SALES}, ADA, shared
    )
    _as(client, VIC)
    got = client.get(f"/charts/api/{fid}/data.csv")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("text/csv")
    assert 'filename="Sales by quarter.csv"' in got.headers["content-disposition"]
    assert got.content.decode("utf-8-sig").splitlines() == ["Quarter,Sales", "Q1,10", "'=cmd,14"]


def test_only_charts_and_diagrams(client: TestClient, shared: int) -> None:
    area = files_pkg.file_area(_db(client))
    fid = area.save(
        ADA,
        "uploads/x.json",
        io.BytesIO(b"{}"),
        filename="x.json",
        mime_type="application/json",
        conversation_id=shared,
    )
    assert client.get(f"/charts/api/{fid}").status_code == 404
    assert client.get("/charts/api/999999").status_code == 404


def test_a_damaged_chart_is_not_drawn(client: TestClient, shared: int) -> None:
    fid = _make(client, "create_chart", {"title": "Sales", "definition": SALES}, ADA, shared)
    from spark.database import files as files_db

    row = _db(client).call(files_db.get_user_file, ADA, fid)
    path = files_pkg.file_store().local_path(ADA, str(row["storage_key"]))
    body = json.loads(path.read_text(encoding="utf-8"))
    body["definition"]["formatter"] = "function(){alert(1)}"
    path.write_text(json.dumps(body), encoding="utf-8")
    got = client.get(f"/charts/api/{fid}")
    assert got.status_code == 422 and "unknown fields: formatter" in got.json()["error"]


def test_a_private_chart_is_its_owners_only(client: TestClient) -> None:
    config = {"embedded_tools": {}, "_user_guid": ADA, "_files_db": _db(client)}
    text, error = execute_builtin_tool(
        "create_chart", {"title": "Mine", "definition": SALES}, config
    )
    assert error is False and parse_card(text) is None
    fid = int(text.split("as file ")[1].split(";")[0])
    assert client.get(f"/charts/api/{fid}").status_code == 200
    _as(client, CORA)
    assert client.get(f"/charts/api/{fid}").status_code == 404
