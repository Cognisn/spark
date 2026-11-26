"""Project cards through the web, in both modes (PC17, PC18).

Server mode (PostgreSQL, warden users, the stub model calling the project tools in real
turns): Ada owns a conversation shared with Cora (Contributor) and Vic (Read-only); Sam
is a stranger.

* A user who asks gets the project at once; a turn whose message does not ask creates
  nothing, however the model calls the tool.
* A suggestion card: only its sender sees what it proposes and acts on it; the other
  members see its kind and state; a stranger gets 404. Create, Edit and Dismiss; replay
  refused; after Dismiss the model's next suggestion is refused.
* An update card waits for Confirm; Cancel changes nothing; Read-only and non-owner
  updates are refused before any card.
* A move card carries the warning with names, and moves on Confirm.
* Every step is audited.

Personal mode (SQLite): the same cards work for the one local user.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from conduit import ModelRequest
from conduit.providers.stub import StubToolCall, StubTurn
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversations
from spark.database import projects as projects_db
from spark.identity import audit as spark_audit
from spark.mode import Mode
from spark.projects import cards
from spark.tools.registry import PERMISSIONS_KEY, execute_builtin_tool
from tests.test_user_scoping_web import client, host, server_db  # noqa: F401

MODEL = "stub/model-a"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def _fetch(client: TestClient, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return client.app.state.sync_db.fetch_value(sql, params)  # type: ignore[attr-defined]


# -- Personal mode ---------------------------------------------------------------------


ME = "u1"


@pytest.fixture
def personal(sqlite_settings: Any, tmp_path: Path) -> Iterator[TestClient]:
    from spark.web.server import create_app
    from tests.db_helpers import app_client
    from tests.test_voice_endpoints import _mock_settings_get
    from tests.web_helpers import sign_in as personal_sign_in

    files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    try:
        with app_client(app, sqlite_settings) as c:
            app.state.user_guid = ME
            personal_sign_in(c)
            yield c
    finally:
        files_pkg.reset()


def _tool(client: TestClient, cid: int, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    config = {
        "embedded_tools": {},
        "_user_guid": ME,
        "_kg_user_guid": ME,
        "_kg_db": client.app.state.sync_db,  # type: ignore[attr-defined]
        "_kg_conversation_id": cid,
        PERMISSIONS_KEY: None,
    }
    return execute_builtin_tool(name, args, config)


def _card_id(text: str) -> str:
    ref = cards.parse_line(text)
    assert ref is not None, text
    return str(ref["card"])


class TestPersonal:
    def test_suggest_edit_create(self, personal: TestClient) -> None:
        cid = _call(personal, conversations.create_conversation, "Garden talk", MODEL, ME)
        text, error = _tool(personal, cid, "suggest_project", {"name": "Garden", "reason": "Often"})
        assert not error, text
        card_id = _card_id(text)
        view = personal.get(f"/project-cards/api/{card_id}").json()["card"]
        assert (view["kind"], view["state"], view["can_act"]) == ("suggestion", "pending", True)
        assert view["payload"]["name"] == "Garden"
        made = personal.post(
            f"/project-cards/api/{card_id}/accept",
            json={"edits": {"name": "Vegetables", "instructions": "Metric."}},
        )
        assert made.status_code == 200, made.text
        pid = made.json()["project_id"]
        project = personal.get(f"/projects/api/{pid}").json()["project"]
        assert (project["name"], project["instructions"]) == ("Vegetables", "Metric.")
        assert made.json()["card"]["state"] == "accepted"
        again = personal.post(f"/project-cards/api/{card_id}/accept", json={})
        assert again.status_code == 409 and again.json()["card"]["state"] == "accepted"
        text, error = _tool(personal, cid, "suggest_project", {"name": "Another"})
        assert error and "only once" in text

    def test_update_and_move(self, personal: TestClient) -> None:
        pid = _call(personal, projects_db.create_project, ME, "Home", instructions="Old.")
        cid = _call(personal, conversations.create_conversation, "Chat", MODEL, ME)
        text, error = _tool(
            personal,
            cid,
            "update_project",
            {"project_id": pid, "changes": {"instructions": "New."}},
        )
        assert not error, text
        card_id = _card_id(text)
        view = personal.get(f"/project-cards/api/{card_id}").json()["card"]
        assert view["payload"]["before"] == {"instructions": "Old."}
        assert view["payload"]["after"] == {"instructions": "New."}
        assert personal.get(f"/projects/api/{pid}").json()["project"]["instructions"] == "Old."
        done = personal.post(f"/project-cards/api/{card_id}/accept", json={})
        assert done.status_code == 200, done.text
        assert personal.get(f"/projects/api/{pid}").json()["project"]["instructions"] == "New."
        # Edits belong to a suggestion only.
        text, _ = _tool(
            personal, cid, "update_project", {"project_id": pid, "changes": {"name": "X"}}
        )
        refused = personal.post(
            f"/project-cards/api/{_card_id(text)}/accept", json={"edits": {"name": "Y"}}
        )
        assert refused.status_code == 400

        text, error = _tool(personal, cid, "move_conversation_to_project", {"project_id": pid})
        assert not error, text
        card_id = _card_id(text)
        view = personal.get(f"/project-cards/api/{card_id}").json()["card"]
        assert view["payload"]["widened"] == {} and view["payload"]["to"]["name"] == "Home"
        cancelled = personal.post(f"/project-cards/api/{card_id}/dismiss")
        assert cancelled.json()["card"]["state"] == "cancelled"
        assert _fetch(personal, "SELECT project_id FROM conversations WHERE id = ?", (cid,)) is None
        text, _ = _tool(personal, cid, "move_conversation_to_project", {"project_id": pid})
        moved = personal.post(f"/project-cards/api/{_card_id(text)}/accept", json={})
        assert moved.status_code == 200 and moved.json()["move"]["moved"] is True
        assert _fetch(personal, "SELECT project_id FROM conversations WHERE id = ?", (cid,)) == pid

    def test_unknown_card(self, personal: TestClient) -> None:
        assert personal.get("/project-cards/api/nothing").status_code == 404
        assert personal.post("/project-cards/api/nothing/accept", json={}).status_code == 404
        assert personal.post("/project-cards/api/nothing/dismiss").status_code == 404


# -- Server mode -----------------------------------------------------------------------


class ToolScript:
    """The stub's answers: the next planned tool call (once), then plain text."""

    def __init__(self) -> None:
        self.planned: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()
        self.count = 0

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        with self.lock:
            if self.planned:
                name, args = self.planned.pop(0)
                self.count += 1
                return StubTurn(
                    tool_calls=(StubToolCall(name=name, input=args, id=f"call-{self.count}"),)
                )
        return StubTurn(text="Done.")


@pytest.fixture
def stack(client: TestClient) -> Any:  # noqa: F811
    from tests.test_conversation_sharing_web import install_runtime

    script = ToolScript()
    install_runtime(client.app, script, ("user", "power_user", "auditor", "administrator"))  # type: ignore[arg-type]
    return client, script


@pytest.fixture
def team(stack: Any, tmp_path: Path) -> Iterator[dict[str, Any]]:
    from spark.identity.permissions import POWER_USER, USER
    from tests.test_conversation_sharing_web import new_conversation, person, share
    from tests.test_projects_web import ADA, CORA, SAM, VIC

    client, script = stack
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "volume")}),
        data_dir=tmp_path,
        runtime_mode=Mode.SERVER,
    )
    people = {
        "ada": person(client, ADA, POWER_USER),
        "cora": person(client, CORA, USER),
        "vic": person(client, VIC, USER),
        "sam": person(client, SAM, USER),
    }
    cid = new_conversation(people["ada"])
    assert share(people["ada"], cid, people["cora"], "contributor").json()["status"] == "added"
    assert share(people["ada"], cid, people["vic"], "viewer").json()["status"] == "added"
    try:
        yield {"client": client, "script": script, "people": people, "cid": cid}
    finally:
        files_pkg.reset()


def _send(
    team: dict[str, Any], who: str, message: str, *planned: tuple[str, dict[str, Any]]
) -> list[str]:
    from tests.test_charts_sharing_web import _results

    team["script"].planned.extend(planned)
    person = team["people"][who]
    sent = person.browser.post(f"/chat/{team['cid']}/api/send", json={"message": message})
    assert sent.status_code == 200, sent.text
    return _results(person, team["cid"])


def _events(team: dict[str, Any], action: str) -> list[Any]:
    from tests.test_conversation_sharing_web import events

    return events(team["client"], action)


def _projects(team: dict[str, Any]) -> list[str]:
    rows = team["client"].app.state.sync_db.fetch_all("SELECT name FROM projects ORDER BY id")
    return [r["name"] for r in rows]


@pytest.mark.postgres
class TestServer:
    def test_created_when_the_user_asks_and_only_then(self, team: dict[str, Any]) -> None:
        call = ("create_project", {"name": "Launch", "requested_by_user": True})
        results = _send(team, "cora", "Summarise the launch notes", call)
        assert "did not ask" in results[-1] and _projects(team) == []
        results = _send(team, "cora", "Please create a project for the launch", call)
        assert "created as project" in results[-1], results
        owner = team["client"].app.state.sync_db.fetch_value("SELECT user_guid FROM projects")
        assert owner == team["people"]["cora"].guid
        assert _events(team, spark_audit.PROJECT_CREATED_BY_TOOL)

    def test_a_suggestion_card_in_a_shared_conversation(self, team: dict[str, Any]) -> None:
        people = team["people"]
        results = _send(
            team, "cora", "Here are my garden notes", ("suggest_project", {"name": "Garden"})
        )
        card_id = _card_id(results[-1])
        url = f"/project-cards/api/{card_id}"
        mine = people["cora"].browser.get(url).json()["card"]
        assert mine["can_act"] and mine["payload"]["name"] == "Garden"
        for who in ("ada", "vic"):
            seen = people[who].browser.get(url).json()["card"]
            assert seen == {
                "id": card_id,
                "kind": "suggestion",
                "state": "pending",
                "conversation_id": team["cid"],
                "mine": False,
                "can_act": False,
            }
        assert people["sam"].browser.get(url).status_code == 404
        for who in ("ada", "vic", "sam"):
            assert people[who].browser.post(f"{url}/accept", json={}).status_code == 404, who
            assert people[who].browser.post(f"{url}/dismiss").status_code == 404, who
        assert _projects(team) == []
        assert _events(team, spark_audit.PROJECT_CARD_SHOWN)

        dismissed = people["cora"].browser.post(f"{url}/dismiss")
        assert dismissed.status_code == 200 and dismissed.json()["card"]["state"] == "dismissed"
        assert people["cora"].browser.post(f"{url}/accept", json={}).status_code == 409
        assert _events(team, spark_audit.PROJECT_CARD_DISMISSED)
        results = _send(team, "ada", "Anything else?", ("suggest_project", {"name": "Again"}))
        assert "only once" in results[-1] and _projects(team) == []

    def test_create_from_a_card(self, team: dict[str, Any]) -> None:
        results = _send(team, "ada", "My notes", ("suggest_project", {"name": "Notes"}))
        url = f"/project-cards/api/{_card_id(results[-1])}"
        made = team["people"]["ada"].browser.post(
            url + "/accept", json={"edits": {"name": "Ada's notes"}}
        )
        assert made.status_code == 200, made.text
        assert _projects(team) == ["Ada's notes"]
        assert team["people"]["ada"].browser.post(url + "/accept", json={}).status_code == 409
        assert _events(team, spark_audit.PROJECT_CARD_ACCEPTED)

    def test_update_cards(self, team: dict[str, Any]) -> None:
        people = team["people"]
        ada = people["ada"].browser
        pid = int(
            ada.post("/projects/api", json={"name": "Plan", "instructions": "Old."}).json()[
                "project"
            ]["id"]
        )
        added = ada.post(
            f"/projects/api/{pid}/members",
            json={"user_id": people["vic"].warden_id, "role": "viewer", "scope": "project_only"},
        )
        assert added.status_code == 200, added.text
        change = ("update_project", {"project_id": pid, "changes": {"instructions": "New."}})
        # Someone who does not own the project is refused before any card.
        results = _send(team, "cora", "Change the Plan instructions", change)
        assert "Nothing was proposed" in results[-1]
        assert not _fetch(team["client"], "SELECT COUNT(*) FROM project_cards")
        results = _send(team, "ada", "Change the Plan instructions", change)
        url = f"/project-cards/api/{_card_id(results[-1])}"
        assert ada.get(f"/projects/api/{pid}").json()["project"]["instructions"] == "Old."
        confirmed = ada.post(url + "/accept", json={})
        assert confirmed.status_code == 200, confirmed.text
        assert ada.get(f"/projects/api/{pid}").json()["project"]["instructions"] == "New."
        assert _events(team, spark_audit.PROJECT_UPDATED_BY_TOOL)
        results = _send(
            team,
            "ada",
            "And the name",
            ("update_project", {"project_id": pid, "changes": {"name": "P2"}}),
        )
        cancel = ada.post(f"/project-cards/api/{_card_id(results[-1])}/dismiss")
        assert cancel.json()["card"]["state"] == "cancelled"
        assert ada.get(f"/projects/api/{pid}").json()["project"]["name"] == "Plan"

    def test_a_move_card_warns_with_names(self, team: dict[str, Any]) -> None:
        people = team["people"]
        ada = people["ada"].browser
        pid = int(ada.post("/projects/api", json={"name": "Plan"}).json()["project"]["id"])
        results = _send(
            team,
            "ada",
            "Move this into Plan",
            ("move_conversation_to_project", {"project_id": pid}),
        )
        url = f"/project-cards/api/{_card_id(results[-1])}"
        view = ada.get(url).json()["card"]
        removed = sorted(view["payload"]["shares_removed"])
        assert removed == sorted([people["cora"].guid, people["vic"].guid])
        assert set(view["payload"]["names"]) == set(removed)
        moved = ada.post(url + "/accept", json={})
        assert moved.status_code == 200 and moved.json()["move"]["moved"], moved.text
        assert _events(team, spark_audit.PROJECT_CONVERSATION_MOVED)
        assert (
            people["cora"].browser.get(f"/conversations/api/{team['cid']}/members").status_code
            == 404
        )
