"""Server mode: projects through the web, end to end (PC9 to PC14).

The real app on a PostgreSQL clone at head with warden's chain, over conduit's stub
provider; synthetic users signed in through warden. Ada owns the project "Plan" with the
sub-project "Detail", shared with Cora (Contributor, all conversations), Vic (Read-only,
all conversations) and Pat (Contributor, project only); Sam is a stranger.

* Each role's allowed and refused project routes (a stranger 404, a role without the
  capability 403); conversations in the project per scope, through the conversation
  routes themselves (members, send).
* Resources: a Contributor's upload is charged to the owner's quota; downloads through
  the project's access check; removal of one's own only.
* The share limit on the conversation share route; scope changes and removal, live.
* Moves with the 409 report and confirmation; deletion with both choices, notices and
  mail; transfer with and without quota; every change audited.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.identity import audit as spark_audit
from spark.identity.permissions import ADMINISTRATOR, POWER_USER, USER
from spark.mode import Mode
from spark.projects import notices
from tests.test_conversation_sharing_web import (  # noqa: F401
    FETCH,
    MODEL,
    Person,
    events,
    new_conversation,
    person,
    script,
    share,
)
from tests.test_user_scoping_web import client, host, server_db  # noqa: F401

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
CORA = "cora@example.com"
VIC = "vic@example.com"
PAT = "pat@example.com"
SAM = "sam@example.com"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def storage(client: TestClient, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """The file area's start-up settings (its root); limits are the administrator's."""
    values: dict[str, Any] = {"files.root": str(tmp_path / "volume")}
    files_pkg.configure(_Settings(values), data_dir=tmp_path, runtime_mode=Mode.SERVER)
    yield values
    files_pkg.reset()


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def capture(secrets: Any, to: str, **kwargs: Any) -> bool:
        sent.append({"to": to, **kwargs})
        return True

    monkeypatch.setattr(notices, "send_project_notice", capture)
    return sent


@pytest.fixture
def people(client: TestClient, script: Any, storage: dict[str, Any]) -> dict[str, Person]:
    return {
        "ada": person(client, ADA, POWER_USER),
        "cora": person(client, CORA, USER),
        "vic": person(client, VIC, USER),
        "pat": person(client, PAT, USER),
        "sam": person(client, SAM, USER),
    }


def add_member(owner: Person, pid: int, who: Person, role: str, scope: str) -> Any:
    return owner.browser.post(
        f"/projects/api/{pid}/members",
        json={"user_id": who.warden_id, "role": role, "scope": scope},
    )


@pytest.fixture
def plan(people: dict[str, Person], mail: list[dict[str, Any]]) -> dict[str, int]:
    ada = people["ada"].browser
    made = ada.post("/projects/api", json={"name": "Plan", "instructions": "Be brief."})
    assert made.status_code == 201, made.text
    top = int(made.json()["project"]["id"])
    sub = ada.post("/projects/api", json={"name": "Detail", "parent_id": top})
    assert sub.status_code == 201, sub.text
    for who, role, scope in (
        ("cora", "contributor", "all_conversations"),
        ("vic", "viewer", "all_conversations"),
        ("pat", "contributor", "project_only"),
    ):
        added = add_member(people["ada"], top, people[who], role, scope)
        assert added.status_code == 200 and added.json()["status"] == "added", added.text
    return {"plan": top, "detail": int(sub.json()["project"]["id"])}


def start(who: Person, pid: int, name: str = "In the project") -> Any:
    return who.browser.post(
        "/conversations/api/create", json={"name": name, "model_id": MODEL, "project_id": pid}
    )


def started(who: Person, pid: int, name: str = "In the project") -> int:
    made = start(who, pid, name)
    assert made.status_code == 200, made.text
    assert made.json()["project_id"] == pid
    cid = int(made.json()["id"])
    saved = who.browser.post(
        f"/chat/{cid}/api/settings", json={"memory_enabled": False, "rag_enabled": False}
    )
    assert saved.status_code == 200, saved.text
    return cid


def upload(who: Person, pid: int, name: str, data: bytes) -> Any:
    return who.browser.post(
        f"/projects/api/{pid}/resources", files={"file": (name, data, "text/plain")}
    )


@pytest.fixture
def admin(people: dict[str, Person], client: TestClient) -> Person:
    return person(client, "root@example.com", ADMINISTRATOR)


def set_quota(admin: Person, megabytes: int) -> None:
    """The administrator's per-user quota, as the Settings page sets it."""
    resp = admin.browser.post("/settings/api/instance", json={"files.quota_mb": megabytes})
    assert resp.status_code == 200, resp.text


def usage(who: Person) -> int:
    return int(who.browser.get("/files/api", headers=FETCH).json()["usage_bytes"])


class TestProjectRoutes:
    def test_each_role_on_the_project(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        for who, expected in (
            ("ada", 200),
            ("cora", 200),
            ("vic", 200),
            ("pat", 200),
            ("sam", 404),
        ):
            got = people[who].browser.get(f"/projects/api/{plan['detail']}", headers=FETCH)
            assert got.status_code == expected, (who, got.text)
        body = people["ada"].browser.get(f"/projects/api/{plan['plan']}", headers=FETCH).json()
        assert body["project"]["role"] == "owner" and len(body["members"]) == 3
        assert [s["name"] for s in body["sub_projects"]] == ["Detail"]
        vic = people["vic"].browser.get(f"/projects/api/{plan['plan']}", headers=FETCH).json()
        assert vic["project"]["role"] == "viewer" and vic["members"] is None
        assert vic["project"]["can_contribute"] is False

    def test_only_the_owner_edits_and_manages(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        for who, expected in (("cora", 403), ("vic", 403), ("sam", 404), ("ada", 200)):
            got = people[who].browser.patch(
                f"/projects/api/{plan['plan']}", json={"instructions": "Changed."}
            )
            assert got.status_code == expected, (who, got.text)
        for who, expected in (("cora", 403), ("sam", 404)):
            got = add_member(people[who], plan["plan"], people["sam"], "viewer", "project_only")
            assert got.status_code == expected, (who, got.text)
            sub = people[who].browser.post(
                "/projects/api", json={"name": "x", "parent_id": plan["plan"]}
            )
            assert sub.status_code == expected
        assert events(people["ada"].browser, spark_audit.PROJECT_UPDATED)

    def test_the_tree_for_owner_and_member(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        ada = people["ada"].browser.get("/projects/api", headers=FETCH).json()
        assert [n["name"] for n in ada["owned"]] == ["Plan"] and ada["shared"] == []
        assert [c["name"] for c in ada["owned"][0]["children"]] == ["Detail"]
        cora = people["cora"].browser.get("/projects/api", headers=FETCH).json()
        assert cora["owned"] == []
        assert [(n["name"], n["role"], n["scope"]) for n in cora["shared"]] == [
            ("Plan", "contributor", "all_conversations")
        ]
        assert people["sam"].browser.get("/projects/api", headers=FETCH).json()["shared"] == []

    def test_a_too_deep_project_is_refused_with_a_message(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        parent = plan["detail"]
        for level in (3, 4, 5):
            made = people["ada"].browser.post(
                "/projects/api", json={"name": f"L{level}", "parent_id": parent}
            )
            parent = int(made.json()["project"]["id"])
        refused = people["ada"].browser.post(
            "/projects/api", json={"name": "L6", "parent_id": parent}
        )
        assert refused.status_code == 400 and "at most 5 levels" in refused.json()["error"]

    def test_sharing_is_audited_and_mailed(
        self, people: dict[str, Person], plan: dict[str, int], mail: list[dict[str, Any]]
    ) -> None:
        assert {m["to"] for m in mail} == {CORA, VIC, PAT}
        assert all(m["kind"] == "shared" for m in mail)
        assert len(events(people["ada"].browser, spark_audit.PROJECT_MEMBER_ADDED)) == 3


class TestConversationsInTheProject:
    def test_who_starts_conversations(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        assert start(people["cora"], plan["detail"]).status_code == 200
        assert start(people["pat"], plan["plan"]).status_code == 200
        assert start(people["vic"], plan["plan"]).status_code == 403
        assert start(people["sam"], plan["plan"]).status_code == 404

    def test_each_scope_through_the_conversation_routes(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        cid = started(people["ada"], plan["detail"])
        for who, expected in (("cora", 200), ("vic", 200), ("pat", 404), ("sam", 404)):
            got = people[who].browser.get(f"/conversations/api/{cid}/members", headers=FETCH)
            assert got.status_code == expected, (who, got.text)
        assert (
            people["cora"]
            .browser.post(f"/chat/{cid}/api/send", json={"message": "from Cora"})
            .status_code
            == 200
        )
        assert (
            people["vic"]
            .browser.post(f"/chat/{cid}/api/send", json={"message": "from Vic"})
            .status_code
            == 403
        )
        # The owner of the project sees a member's conversation (question 89).
        pats = started(people["pat"], plan["plan"])
        assert (
            people["ada"]
            .browser.get(f"/conversations/api/{pats}/members", headers=FETCH)
            .status_code
            == 200
        )
        tree = people["vic"].browser.get("/projects/api", headers=FETCH).json()
        seen = {c["id"] for n in tree["shared"] for c in n["conversations"]}
        seen |= {
            c["id"] for n in tree["shared"] for ch in n["children"] for c in ch["conversations"]
        }
        assert seen == {cid, pats}

    def test_scope_change_and_removal_take_effect_at_once(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        cid = started(people["ada"], plan["plan"])
        narrowed = add_member(
            people["ada"], plan["plan"], people["cora"], "contributor", "project_only"
        )
        assert narrowed.json()["status"] == "changed"
        assert (
            people["cora"]
            .browser.get(f"/conversations/api/{cid}/members", headers=FETCH)
            .status_code
            == 404
        )
        gone = people["ada"].browser.delete(
            f"/projects/api/{plan['plan']}/members/{people['vic'].guid}"
        )
        assert gone.status_code == 200
        assert (
            people["vic"].browser.get(f"/projects/api/{plan['plan']}", headers=FETCH).status_code
            == 404
        )
        assert (
            people["vic"]
            .browser.get(f"/conversations/api/{cid}/members", headers=FETCH)
            .status_code
            == 404
        )
        assert events(people["ada"].browser, spark_audit.PROJECT_MEMBER_CHANGED)
        assert events(people["ada"].browser, spark_audit.PROJECT_MEMBER_REMOVED)

    def test_the_share_limit_on_the_share_route(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        cid = started(people["ada"], plan["plan"])
        refused = share(people["ada"], cid, people["sam"], "viewer")
        assert refused.status_code == 400 and "access to the project" in refused.json()["error"]
        assert share(people["ada"], cid, people["pat"], "viewer").status_code == 200
        # An individual share survives Pat's scope staying project only.
        assert (
            people["pat"]
            .browser.get(f"/conversations/api/{cid}/members", headers=FETCH)
            .status_code
            == 200
        )

    def test_the_context_route(self, people: dict[str, Person], plan: dict[str, int]) -> None:
        cid = started(people["cora"], plan["detail"])
        assert upload(people["ada"], plan["plan"], "brief.txt", b"brief").status_code == 201
        got = (
            people["vic"]
            .browser.get(f"/projects/api/conversations/{cid}/context", headers=FETCH)
            .json()
        )
        assert [p["name"] for p in got["path"]] == ["Plan", "Detail"]
        assert [r["filename"] for r in got["levels"][0]["resources"]] == ["brief.txt"]
        assert (
            people["sam"]
            .browser.get(f"/projects/api/conversations/{cid}/context", headers=FETCH)
            .status_code
            == 404
        )


class TestResources:
    def test_a_contributors_upload_is_charged_to_the_owner(
        self, people: dict[str, Person], plan: dict[str, int]
    ) -> None:
        made = upload(people["cora"], plan["detail"], "notes.txt", b"x" * 1000)
        assert made.status_code == 201, made.text
        assert usage(people["ada"]) == 1000
        assert usage(people["cora"]) == 0
        assert upload(people["vic"], plan["plan"], "v.txt", b"v").status_code == 403
        assert upload(people["sam"], plan["plan"], "s.txt", b"s").status_code == 404
        fid = made.json()["resource"]["file_id"]
        url = f"/projects/api/{plan['detail']}/resources/{fid}"
        assert people["vic"].browser.get(url).content == b"x" * 1000
        assert people["sam"].browser.get(url).status_code == 404
        # The file is not reachable through Cora's own file routes: it is Ada's.
        assert people["cora"].browser.get(f"/files/api/{fid}").status_code == 404
        assert events(people["ada"].browser, spark_audit.PROJECT_RESOURCE_ADDED)

    def test_the_owners_quota_refuses_a_contributors_upload(
        self, people: dict[str, Person], plan: dict[str, int], admin: Person
    ) -> None:
        set_quota(admin, 1)
        refused = upload(people["cora"], plan["plan"], "big.txt", b"x" * 1_200_000)
        assert refused.status_code == 507 and "owner's storage" in refused.json()["error"]

    def test_removing_ones_own_only(self, people: dict[str, Person], plan: dict[str, int]) -> None:
        adas = upload(people["ada"], plan["plan"], "a.txt", b"aa").json()["resource"]["file_id"]
        coras = upload(people["cora"], plan["plan"], "c.txt", b"cc").json()["resource"]["file_id"]
        base = f"/projects/api/{plan['plan']}/resources"
        assert people["cora"].browser.delete(f"{base}/{adas}").status_code == 403
        assert people["vic"].browser.delete(f"{base}/{coras}").status_code == 403
        assert people["cora"].browser.delete(f"{base}/{coras}").status_code == 200
        assert people["ada"].browser.delete(f"{base}/{adas}").status_code == 200
        assert usage(people["ada"]) == 0


class TestMovesDeletionAndTransfer:
    def test_a_move_asks_first(self, people: dict[str, Person], plan: dict[str, int]) -> None:
        cid = new_conversation(people["ada"])
        url = f"/projects/api/conversations/{cid}/move"
        asked = people["ada"].browser.post(url, json={"project_id": plan["plan"]})
        assert asked.status_code == 409
        assert set(asked.json()["widened"]) == {people["cora"].guid, people["vic"].guid}
        assert asked.json()["names"][people["cora"].guid] == CORA
        done = people["ada"].browser.post(url, json={"project_id": plan["plan"], "confirm": True})
        assert done.status_code == 200 and done.json()["moved"]
        assert people["cora"].browser.post(url, json={"project_id": None}).status_code == 403
        assert people["sam"].browser.post(url, json={"project_id": None}).status_code == 404
        assert events(people["ada"].browser, spark_audit.PROJECT_CONVERSATION_MOVED)

    def test_delete_everything_tells_the_starters(
        self, people: dict[str, Person], plan: dict[str, int], mail: list[dict[str, Any]]
    ) -> None:
        started(people["ada"], plan["plan"])
        coras = started(people["cora"], plan["detail"])
        preview = (
            people["ada"]
            .browser.get(f"/projects/api/{plan['plan']}/delete-preview", headers=FETCH)
            .json()
        )
        assert preview["conversations"] == 2 and preview["conversations_by_others"] == 1
        assert preview["members"] == 3 and preview["sub_projects"] == 1
        assert (
            people["cora"]
            .browser.post(f"/projects/api/{plan['plan']}/delete", json={"mode": "everything"})
            .status_code
            == 403
        )
        done = people["ada"].browser.post(
            f"/projects/api/{plan['plan']}/delete", json={"mode": "everything"}
        )
        assert done.status_code == 200 and done.json()["conversations_deleted"] == 2
        told = people["cora"].browser.get("/projects/api/notices", headers=FETCH).json()
        assert [(n["kind"], n["count"]) for n in told["notices"]] == [("project_deleted", 1)]
        assert any(m["kind"] == "deleted" and m["to"] == CORA for m in mail)
        assert coras not in {
            c["id"]
            for c in people["cora"].browser.get("/conversations/api/list", headers=FETCH).json()
        }
        assert events(people["ada"].browser, spark_audit.PROJECT_DELETED)

    def test_delete_and_keep(self, people: dict[str, Person], plan: dict[str, int]) -> None:
        coras = started(people["cora"], plan["detail"])
        done = people["ada"].browser.post(
            f"/projects/api/{plan['plan']}/delete", json={"mode": "keep"}
        )
        assert done.status_code == 200 and done.json()["conversations_kept"] == 1
        assert (
            people["cora"]
            .browser.get(f"/conversations/api/{coras}/members", headers=FETCH)
            .status_code
            == 200
        )
        assert (
            people["ada"]
            .browser.get(f"/conversations/api/{coras}/members", headers=FETCH)
            .status_code
            == 404
        )

    def test_transfer_with_and_without_quota(
        self,
        people: dict[str, Person],
        plan: dict[str, int],
        admin: Person,
        mail: list[dict[str, Any]],
    ) -> None:
        upload(people["ada"], plan["plan"], "data.txt", b"d" * 100_000)
        set_quota(admin, 1)
        held = people["cora"].browser.post(
            "/files/api", files={"file": ("held.bin", b"h" * 1_000_000, "text/plain")}
        )
        assert held.status_code == 201, held.text  # Cora's area is nearly full
        url = f"/projects/api/{plan['plan']}/transfer"
        refused = people["ada"].browser.post(url, json={"user_guid": people["cora"].guid})
        assert refused.status_code == 507
        set_quota(admin, 2)
        assert (
            people["cora"].browser.post(url, json={"user_guid": people["vic"].guid}).status_code
            == 403
        )
        done = people["ada"].browser.post(url, json={"user_guid": people["cora"].guid})
        assert done.status_code == 200, done.text
        assert usage(people["cora"]) == 1_100_000 and usage(people["ada"]) == 0
        body = people["cora"].browser.get(f"/projects/api/{plan['plan']}", headers=FETCH).json()
        assert body["project"]["role"] == "owner"
        fid = body["resources"][0]["file_id"]
        assert (
            people["vic"].browser.get(f"/projects/api/{plan['plan']}/resources/{fid}").content
            == b"d" * 100_000
        )
        ada = people["ada"].browser.get(f"/projects/api/{plan['plan']}", headers=FETCH).json()
        assert (ada["project"]["role"], ada["project"]["scope"]) == (
            "contributor",
            "all_conversations",
        )
        assert any(m["kind"] == "transferred" and m["to"] == CORA for m in mail)
        assert events(people["ada"].browser, spark_audit.PROJECT_TRANSFERRED)

    def test_csrf_is_required(self, people: dict[str, Person], plan: dict[str, int]) -> None:
        from spark.web.security import CSRF_HEADER

        browser = people["ada"].browser
        token = browser.headers.pop(CSRF_HEADER)
        try:
            assert browser.post("/projects/api", json={"name": "x"}).status_code == 403
        finally:
            browser.headers[CSRF_HEADER] = token
