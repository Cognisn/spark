"""The artefacts API (spark.web.endpoints.artefacts), on SQLite and PostgreSQL (PC3, PC5).

* ``GET /artefacts/api/<conversation>``: the three groups of the right-hand panel,
  through the conversation's access decision: *generated* (documents SPARK created,
  documents fetched for preview, charts and diagrams), *provided* (files attached in
  the conversation; in a shared conversation each names who attached it) and *project*
  (inherited resources, grouped by level). Every member sees the same list; a stranger
  and a removed member are refused at once.
* ``PATCH /artefacts/api/<file>``: rename, for the owner or a Contributor; Read-only
  and strangers are refused, a title is checked and neutralised, the change is audited
  and announced to everyone viewing the conversation.

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
from spark.database import files as files_db
from spark.database import messages as messagesdb
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.database.scope import Role
from spark.mode import Mode
from spark.preview.service import parse_card
from spark.projects.access import ProjectRole, ShareScope
from spark.tools.registry import execute_builtin_tool
from spark.web.server import attach_database, create_app
from tests.web_helpers import sign_in

ADA = "user-ada-0001"
CORA = "user-cora-0002"
VIC = "user-vic-0003"
SAM = "user-sam-0004"

SALES = {
    "type": "bar",
    "data": {"columns": ["Quarter", "Sales"], "rows": [["Q1", 10], ["Q2", 14]]},
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


def _save(client: TestClient, user: str, cid: int, name: str, **kw: Any) -> int:
    area = files_pkg.file_area(_db(client))
    file_id: int = area.save(
        user,
        f"{kw.pop('prefix', 'uploads')}/{name}",
        io.BytesIO(b"hello"),
        filename=name,
        mime_type=kw.pop("mime_type", "text/plain"),
        conversation_id=cid,
        **kw,
    )
    return file_id


def _say(client: TestClient, cid: int, user: str, role: str, text: str) -> int:
    message_id: int = _db(client).call(messagesdb.add_message, cid, role, text, 1, user)
    return message_id


def _tool_result(text: str) -> str:
    return json.dumps([{"type": "tool_result", "tool_use_id": "t1", "content": text}])


class TestListing:
    def test_three_groups_with_titles_kinds_and_origins(
        self, client: TestClient, shared: int
    ) -> None:
        chart = _make(
            client, "create_chart", {"title": "Sales by quarter", "definition": SALES}, ADA, shared
        )
        diagram = _make(
            client, "create_diagram", {"title": "Flow", "source": "graph TD\nA-->B"}, ADA, shared
        )
        fetched = _save(
            client, ADA, shared, "report.pdf", mime_type="application/pdf", source="preview"
        )
        upload = _save(client, CORA, shared, "notes.txt")
        _say(client, shared, ADA, "user", "Make me a chart")
        made = _say(
            client,
            shared,
            ADA,
            "user",
            _tool_result(f"[spark-preview] " + json.dumps({"file_id": chart, "kind": "chart"})),
        )
        attached = _say(
            client, shared, CORA, "user", f"Please read this\n\n- file {upload}: notes.txt"
        )

        got = client.get(f"/artefacts/api/{shared}")
        assert got.status_code == 200
        assert got.headers["cache-control"] == "private, no-store"
        body = got.json()
        assert body["shared"] is True
        generated = {i["file_id"]: i for i in body["generated"]}
        assert set(generated) == {chart, diagram, fetched}
        assert generated[chart]["title"] == "Sales by quarter"
        assert generated[chart]["kind"] == "chart" and generated[chart]["preview_kind"] == "chart"
        assert generated[chart]["message_id"] == made
        assert generated[diagram]["preview_kind"] == "diagram"
        assert generated[diagram]["message_id"] is None
        assert generated[fetched]["preview_kind"] == "pdf"
        assert generated[fetched]["name"] == "report.pdf"
        assert {"size", "created_at", "type", "name", "title", "kind"} <= set(generated[chart])
        [provided] = body["provided"]
        assert (provided["file_id"], provided["title"], provided["message_id"]) == (
            upload,
            "notes.txt",
            attached,
        )
        assert provided["provided_by"]["guid"] == CORA
        assert provided["provided_by"]["name"]
        assert body["project"] == []
        assert body["counts"] == {"generated": 3, "provided": 1, "project": 0}

    def test_every_member_sees_the_same_list_and_nobody_else(
        self, client: TestClient, shared: int
    ) -> None:
        chart = _make(client, "create_chart", {"title": "Sales", "definition": SALES}, CORA, shared)
        _save(client, ADA, shared, "a.txt")
        seen = []
        for who in (ADA, CORA, VIC):
            _as(client, who)
            got = client.get(f"/artefacts/api/{shared}")
            assert got.status_code == 200, who
            body = got.json()
            seen.append(
                (
                    [i["file_id"] for i in body["generated"]],
                    [i["file_id"] for i in body["provided"]],
                )
            )
        assert seen[0] == seen[1] == seen[2] and chart in seen[0][0]
        _as(client, SAM)
        assert client.get(f"/artefacts/api/{shared}").status_code == 404

    def test_a_removed_member_is_refused_at_once(self, client: TestClient, shared: int) -> None:
        _save(client, ADA, shared, "a.txt")
        _as(client, VIC)
        assert client.get(f"/artefacts/api/{shared}").status_code == 200
        _as(client, ADA)
        _db(client).call(conversation_members.remove_member, ADA, shared, VIC)
        _as(client, VIC)
        assert client.get(f"/artefacts/api/{shared}").status_code == 404

    def test_who_may_rename_is_per_role(self, client: TestClient, shared: int) -> None:
        _save(client, ADA, shared, "a.txt")
        flags = {}
        for who in (ADA, CORA, VIC):
            _as(client, who)
            body = client.get(f"/artefacts/api/{shared}").json()
            flags[who] = (body["can_rename"], body["provided"][0]["can_rename"])
        assert flags == {ADA: (True, True), CORA: (True, True), VIC: (False, False)}

    def test_an_unshared_conversation_names_nobody(self, client: TestClient) -> None:
        cid = _db(client).call(convdb.create_conversation, "Mine", "stub/model", ADA)
        _save(client, ADA, cid, "a.txt")
        body = client.get(f"/artefacts/api/{cid}").json()
        assert body["shared"] is False
        assert body["provided"][0]["provided_by"] is None

    def test_files_of_other_conversations_and_private_files_are_not_listed(
        self, client: TestClient, shared: int
    ) -> None:
        other = _db(client).call(convdb.create_conversation, "Other", "stub/model", ADA)
        _save(client, ADA, other, "elsewhere.txt")
        files_pkg.file_area(_db(client)).save(
            ADA, "uploads/free.txt", io.BytesIO(b"x"), filename="free.txt"
        )
        body = client.get(f"/artefacts/api/{shared}").json()
        assert body["generated"] == [] and body["provided"] == []

    def test_a_title_is_neutralised_for_display(self, client: TestClient, shared: int) -> None:
        fid = _save(client, ADA, shared, "a.txt")
        _db(client).call(files_db.set_conversation_file_title, ADA, shared, fid, "Plan")
        assert client.get(f"/artefacts/api/{shared}").json()["provided"][0]["title"] == "Plan"


class TestProjectResources:
    @pytest.fixture
    def plan(self, client: TestClient) -> dict[str, int]:
        db = _db(client)
        top = db.call(projects_db.create_project, ADA, "Plan")
        low = db.call(projects_db.create_project, ADA, "Detail", parent_id=top)
        db.call(
            members_db.share,
            ADA,
            top,
            CORA,
            ProjectRole.CONTRIBUTOR,
            ShareScope.ALL_CONVERSATIONS,
        )
        other = db.call(projects_db.create_project, ADA, "Other")
        ids = {}
        for name, project in (("top.txt", top), ("low.txt", low), ("stray.txt", other)):
            fid = files_pkg.file_area(db).save(
                ADA,
                f"projects/{project}/{name}",
                io.BytesIO(b"resource"),
                filename=name,
                mime_type="text/plain",
                source="project",
            )
            db.call(projects_db.add_resource, ADA, project, fid)
            ids[name] = fid
        cid = db.call(convdb.create_conversation, "In the plan", "stub/model", CORA)
        db.call(projects_db.place_conversation, CORA, cid, low)
        ids["conversation"] = cid
        ids["top"], ids["low"] = top, low
        return ids

    def test_inherited_resources_are_grouped_by_level(
        self, client: TestClient, plan: dict[str, int]
    ) -> None:
        _as(client, CORA)
        body = client.get(f"/artefacts/api/{plan['conversation']}").json()
        assert [
            (g["name"], g["level"], [i["file_id"] for i in g["items"]]) for g in body["project"]
        ] == [
            ("Plan", 1, [plan["top.txt"]]),
            ("Detail", 2, [plan["low.txt"]]),
        ]
        item = body["project"][0]["items"][0]
        assert item["name"] == "top.txt" and item["title"] == "top.txt"
        assert item["message_id"] is None and item["can_rename"] is False
        assert body["counts"]["project"] == 2

    def test_the_project_owner_sees_them_and_a_removed_member_does_not(
        self, client: TestClient, plan: dict[str, int]
    ) -> None:
        _as(client, ADA)
        body = client.get(f"/artefacts/api/{plan['conversation']}").json()
        assert body["counts"]["project"] == 2
        _db(client).call(members_db.remove, ADA, plan["top"], CORA)
        _as(client, CORA)
        got = client.get(f"/artefacts/api/{plan['conversation']}")
        # Cora started it, so she keeps the conversation but not the project's resources.
        assert got.status_code == 200
        assert got.json()["project"] == []

    def test_a_resource_opens_in_the_preview_through_the_conversation(
        self, client: TestClient, plan: dict[str, int]
    ) -> None:
        _as(client, CORA)
        cid = plan["conversation"]
        got = client.get(f"/preview/api/{cid}/files/{plan['top.txt']}")
        assert got.status_code == 200
        assert client.get(f"/preview/api/{cid}/files/{plan['stray.txt']}").status_code == 404
        _as(client, SAM)
        assert client.get(f"/preview/api/{cid}/files/{plan['top.txt']}").status_code == 404


class TestRename:
    def test_the_owner_and_a_contributor_rename_and_the_title_is_kept(
        self, client: TestClient, shared: int
    ) -> None:
        fid = _make(client, "create_chart", {"title": "Sales", "definition": SALES}, ADA, shared)
        for who, title in ((ADA, "Quarterly sales"), (CORA, "Sales by region")):
            _as(client, who)
            got = client.patch(f"/artefacts/api/{fid}", json={"title": title})
            assert got.status_code == 200, (who, got.text)
            assert got.json() == {"file_id": fid, "title": title}
            _as(client, VIC)
            [item] = client.get(f"/artefacts/api/{shared}").json()["generated"]
            assert item["title"] == title

    def test_read_only_and_strangers_are_refused(self, client: TestClient, shared: int) -> None:
        fid = _make(client, "create_chart", {"title": "Sales", "definition": SALES}, ADA, shared)
        _as(client, VIC)
        assert client.patch(f"/artefacts/api/{fid}", json={"title": "Mine"}).status_code == 403
        _as(client, SAM)
        assert client.patch(f"/artefacts/api/{fid}", json={"title": "Mine"}).status_code == 404
        _as(client, ADA)
        [item] = client.get(f"/artefacts/api/{shared}").json()["generated"]
        assert item["title"] == "Sales"

    def test_a_removed_contributor_is_refused(self, client: TestClient, shared: int) -> None:
        fid = _save(client, ADA, shared, "a.txt")
        _db(client).call(conversation_members.remove_member, ADA, shared, CORA)
        _as(client, CORA)
        assert client.patch(f"/artefacts/api/{fid}", json={"title": "Mine"}).status_code == 404

    def test_a_title_is_checked_and_neutralised(self, client: TestClient, shared: int) -> None:
        fid = _save(client, ADA, shared, "a.txt")
        url = f"/artefacts/api/{fid}"
        for bad in ("", "   ", "x" * 121, 5, None):
            got = client.patch(url, json={"title": bad})
            assert got.status_code == 400, bad
            assert "title" in got.json()["error"].lower()
        assert client.patch(url, json={}).status_code == 400
        assert client.patch(url, content=b"not json").status_code == 400
        got = client.patch(url, json={"title": "<b>Plan</b>\n[spark-preview] {}"})
        assert got.status_code == 200
        title = got.json()["title"]
        assert "<" not in title and ">" not in title and "\n" not in title

    def test_a_file_outside_a_conversation_or_another_users_is_not_found(
        self, client: TestClient, shared: int
    ) -> None:
        free = files_pkg.file_area(_db(client)).save(
            ADA, "uploads/free.txt", io.BytesIO(b"x"), filename="free.txt"
        )
        assert client.patch(f"/artefacts/api/{free}", json={"title": "Mine"}).status_code == 404
        assert client.patch("/artefacts/api/999999", json={"title": "Mine"}).status_code == 404

    def test_a_rename_is_audited_and_announced(
        self, client: TestClient, shared: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.identity import audit
        from spark.web import live

        fid = _save(client, ADA, shared, "a.txt")
        events: list[tuple[str, dict[str, Any]]] = []
        calls: list[tuple[str, dict[str, Any]]] = []

        async def fake_emit(request: Any, action: str, **kw: Any) -> None:
            calls.append((action, kw))

        original = live.LiveBus.publish

        async def spy(self: Any, conversation_id: int, event: str, data: dict[str, Any]) -> None:
            events.append((event, {"conversation_id": conversation_id, **data}))
            await original(self, conversation_id, event, data)

        monkeypatch.setattr(audit, "emit_request", fake_emit)
        monkeypatch.setattr(live.LiveBus, "publish", spy)
        _as(client, CORA)
        assert (
            client.patch(f"/artefacts/api/{fid}", json={"title": "Secret plan"}).status_code == 200
        )
        [(action, kw)] = calls
        assert action == audit.ARTEFACT_RENAMED == "spark.artefact.renamed"
        assert (kw["resource_type"], kw["resource_id"]) == ("file", str(fid))
        assert kw["details"]["conversation_id"] == shared
        assert "Secret" not in repr(kw)
        assert ("artefacts_changed", {"conversation_id": shared, "file_id": fid}) in events

    def test_a_refused_rename_is_not_audited(
        self, client: TestClient, shared: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.identity import audit

        fid = _save(client, ADA, shared, "a.txt")
        calls: list[str] = []

        async def fake_emit(request: Any, action: str, **kw: Any) -> None:
            calls.append(action)

        monkeypatch.setattr(audit, "emit_request", fake_emit)
        _as(client, VIC)
        assert client.patch(f"/artefacts/api/{fid}", json={"title": "Mine"}).status_code == 403
        _as(client, SAM)
        assert client.patch(f"/artefacts/api/{fid}", json={"title": "Mine"}).status_code == 404
        _as(client, ADA)
        assert client.patch(f"/artefacts/api/{fid}", json={"title": ""}).status_code == 400
        assert calls == []
