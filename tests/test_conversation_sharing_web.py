"""Server mode: sharing a conversation through the web, end to end.

The real app on a PostgreSQL clone at head with warden's chain, over conduit's stub
provider; synthetic users signed in through warden. Ada owns a conversation and
shares it with Cora (contributor) and Vic (read-only); Sam is a stranger.

* Each role's allowed and refused actions on every conversation route: a stranger
  gets 404, a member without the capability 403 (mutation-checked: each refusal
  fails when the role table or the route's capability is changed).
* Sharing, role changes, removal and transfer, each audited with the client address;
  the Shared with me list, its count and new markers; the notice mail.
* A turn is its sender's: their messages, their usage; a viewer's send, a model the
  sender may not use, and a send while another's turn runs are refused and audited.
* One turn at a time, and live views: a viewer follows another's turn and is cut off
  at once when removed; a removed sender's turn is cancelled.
* Privacy (CS31): linked conversations, knowledge graph builds and the owner's files
  stay the owner's; attachments in the conversation and export are every member's.
* Two replicas on one database: the turn lock holds across them, and a viewer on one
  follows a turn running on the other.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conduit import ModelRequest
from conduit.providers.stub import StubTurn
from fastapi.testclient import TestClient
from warden.audit import AuditQuery, list_events
from warden.audit.store import AuditRow

from spark.connections import ConnectionModel
from spark.database import conversations, messages
from spark.database.connections import Assignment, OverlayRow
from spark.identity import audit as spark_audit
from spark.identity.db import warden_database
from spark.identity.permissions import POWER_USER, USER
from spark.mode import DatabaseSettings
from tests.identity_web_helpers import (
    PUBLIC,
    another_client,
    make_user,
    server_client,
    sign_in,
    write_config,
)
from tests.model_helpers import stub_connection, stub_runtime
from tests.test_user_scoping_web import _call, client, host, server_db  # noqa: F401

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
CORA = "cora@example.com"
VIC = "vic@example.com"
SAM = "sam@example.com"
MODEL = "stub/model-a"
FETCH = {"Accept": "application/json"}


class Script:
    """The stub's answers: plain text, held while ``hold`` is cleared (a long turn)."""

    def __init__(self) -> None:
        self.hold = threading.Event()
        self.hold.set()
        self.waiting = threading.Event()

    def __call__(self, request: ModelRequest, index: int) -> StubTurn:
        if not self.hold.is_set():
            self.waiting.set()
            self.hold.wait(30)
        return StubTurn(text="the answer")


def install_runtime(app: Any, script: Script, roles: tuple[str, ...]) -> None:
    """The app's connection runtime (one stub connection, assigned to ``roles``) and manager."""
    from spark.core.conversation_manager import ConversationManager

    runtime = stub_runtime(
        app.state.sync_db,
        connections=[stub_connection("stub", script="held")],
        scripts={"held": script},
        assignments=[Assignment("stub", role=role) for role in roles],
        overlays=[
            OverlayRow(
                "stub",
                "model-a",
                200_000,
                "local",
                output_limit=8_192,
                tools=True,
                connection_id="stub",
            )
        ],
        models=[ConnectionModel("stub", "model-a")],
    )
    app.state.connections = runtime
    app.state.conversation_manager = ConversationManager(
        app.state.sync_db, runtime, user_guid="process-guid"
    )


@dataclass
class Person:
    browser: TestClient
    guid: str
    warden_id: str


# One address per signed-in browser, from the three documentation ranges, so each is distinct
# (and audited as its own) however many people the session signs in.
_ADDRESSES = iter(
    f"{net}.{host}" for net in ("192.0.2", "198.51.100", "203.0.113") for host in range(10, 250)
)


def person(client: TestClient, email: str, *roles: str) -> Person:
    """A signed-in browser of its own, from its own documentation address (audited)."""
    warden_id = make_user(client, email, *roles)
    browser = TestClient(client.app, base_url=PUBLIC, client=(next(_ADDRESSES), 50000))
    browser.portal = client.portal  # type: ignore[attr-defined]
    assert sign_in(browser, email).status_code == 303
    guid = str(browser.get("/auth/session", headers=FETCH).json()["user_guid"])
    return Person(browser, guid, str(warden_id))


def events(client: TestClient, action: str) -> list[AuditRow]:
    app: Any = client.app

    async def read() -> list[AuditRow]:
        recorder = spark_audit.current()
        if recorder is not None:
            await recorder.drain()
        page = await list_events(warden_database(app.state.database), AuditQuery(actions=(action,)))
        return page.rows

    rows: list[AuditRow] = client.portal.call(read)  # type: ignore[attr-defined]
    return rows


@pytest.fixture
def script(client: TestClient) -> Script:
    held = Script()
    install_runtime(client.app, held, ("user", "power_user", "auditor", "administrator"))
    return held


@pytest.fixture
def people(client: TestClient, script: Script) -> dict[str, Person]:
    return {
        "ada": person(client, ADA, POWER_USER),
        "cora": person(client, CORA, USER),
        "vic": person(client, VIC, USER),
        "sam": person(client, SAM, USER),
    }


def new_conversation(owner: Person, name: str = "Ada's plan") -> int:
    made = owner.browser.post("/conversations/api/create", json={"name": name, "model_id": MODEL})
    assert made.status_code == 200, made.text
    cid = int(made.json()["id"])
    # No memory or retrieval: no embedding model needed.
    saved = owner.browser.post(
        f"/chat/{cid}/api/settings", json={"memory_enabled": False, "rag_enabled": False}
    )
    assert saved.status_code == 200, saved.text
    return cid


def share(owner: Person, cid: int, who: Person, role: str) -> Any:
    return owner.browser.post(
        f"/conversations/api/{cid}/members", json={"user_id": who.warden_id, "role": role}
    )


@pytest.fixture
def shared(people: dict[str, Person]) -> int:
    cid = new_conversation(people["ada"])
    assert share(people["ada"], cid, people["cora"], "contributor").json()["status"] == "added"
    assert share(people["ada"], cid, people["vic"], "viewer").json()["status"] == "added"
    sent = people["ada"].browser.post(f"/chat/{cid}/api/send", json={"message": "hello all"})
    assert sent.status_code == 200, sent.text
    return cid


def wait_for(check: Any, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def turn_row(client: TestClient, cid: int) -> Any:
    return client.app.state.sync_db.fetch_one(  # type: ignore[attr-defined]
        "SELECT user_guid FROM conversation_turns WHERE conversation_id = ?", (cid,)
    )


class TestRoles:
    READ = [
        ("get", "/chat/{cid}"),
        ("get", "/chat/{cid}/api/history"),
        ("get", "/chat/{cid}/api/tool-activity"),
        ("get", "/chat/{cid}/api/agent-history"),
        ("get", "/chat/{cid}/api/info"),
        ("get", "/chat/{cid}/api/tools"),
        ("get", "/chat/{cid}/api/export?format=json"),
        ("get", "/conversations/api/{cid}/members"),
    ]
    MANAGE = [
        ("post", "/chat/{cid}/api/settings", {"instructions": "be brief"}),
        ("post", "/chat/{cid}/api/tools", {"type": "embedded", "name": "x", "enabled": False}),
        ("get", "/chat/{cid}/api/links", None),
        ("post", "/conversations/api/{cid}/rename", {"name": "Renamed"}),
        ("post", "/conversations/api/{cid}/model", {"model_id": MODEL}),
    ]

    def test_reading_is_every_members(self, people: dict[str, Person], shared: int) -> None:
        for method, path in self.READ:
            url = path.format(cid=shared)
            for who in ("ada", "cora", "vic"):
                resp = getattr(people[who].browser, method)(url, headers=FETCH)
                assert resp.status_code == 200, (who, url, resp.text)
            stranger = getattr(people["sam"].browser, method)(url, headers=FETCH)
            if url == f"/chat/{shared}":
                assert "Ada's plan" not in stranger.text  # the page shell, nothing of Ada's
            else:
                assert stranger.status_code == 404, (url, stranger.text)

    def test_managing_is_the_owners(self, people: dict[str, Person], shared: int) -> None:
        for method, path, body in self.MANAGE:
            url = path.format(cid=shared)
            call = getattr(people["cora"].browser, method)
            kwargs = {"json": body} if body is not None else {}
            assert call(url, **kwargs).status_code == 403, url
            assert getattr(people["vic"].browser, method)(url, **kwargs).status_code == 403, url
            assert getattr(people["sam"].browser, method)(url, **kwargs).status_code == 404, url
            owner = getattr(people["ada"].browser, method)(url, **kwargs)
            assert owner.status_code == 200, (url, owner.text)
        for who, status in (("cora", 403), ("vic", 403), ("sam", 404)):
            browser = people[who].browser
            assert share(people[who], shared, people["sam"], "viewer").status_code == status
            assert (
                browser.post(
                    f"/conversations/api/{shared}/transfer", json={"user_guid": people[who].guid}
                ).status_code
                == status
            )
            assert browser.delete(f"/conversations/api/{shared}").status_code == status
        conv = _call(
            people["ada"].browser, conversations.get_conversation, shared, people["ada"].guid
        )
        assert conv is not None and conv["is_active"] == 1 and conv["name"] == "Renamed"

    def test_sending_is_the_owners_and_contributors(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        url = f"/chat/{shared}/api/send"
        assert people["cora"].browser.post(url, json={"message": "from cora"}).status_code == 200
        assert people["vic"].browser.post(url, json={"message": "from vic"}).status_code == 403
        assert people["sam"].browser.post(url, json={"message": "from sam"}).status_code == 404
        stream = people["vic"].browser.get(f"/stream/chat?message=hi&conversation_id={shared}")
        assert stream.status_code == 403
        said = [
            m["content"] for m in _call(client, messages.get_messages, people["ada"].guid, shared)
        ]
        assert "from cora" in said and "from vic" not in said
        refused = events(client, spark_audit.CONVERSATION_SEND_REFUSED)
        reasons = sorted(r.details["reason"] for r in refused)
        assert reasons == ["role", "role"]
        assert all(r.source_ip for r in refused)

    def test_the_page_shows_share_to_the_owner_only(
        self, people: dict[str, Person], shared: int
    ) -> None:
        owner = people["ada"].browser.get(f"/chat/{shared}").text
        viewer = people["vic"].browser.get(f"/chat/{shared}").text
        assert 'id="btn-share"' in owner
        assert 'id="btn-share"' not in viewer
        info = people["vic"].browser.get(f"/chat/{shared}/api/info").json()
        assert (info["my_role"], info["may_send"], info["may_manage"]) == ("viewer", False, False)
        assert info["shared"] and [m["role"] for m in info["members"]] == [
            "owner",
            "contributor",
            "viewer",
        ]
        assert [m["colour"] for m in info["members"]] == [1, 2, 3]
        assert people["cora"].browser.get(f"/chat/{shared}/api/info").json()["may_send"]


class TestSharing:
    def test_people_search(self, people: dict[str, Person]) -> None:
        found = people["ada"].browser.get("/conversations/api/people?q=cor").json()
        assert [p["email"] for p in found] == [CORA]
        assert people["ada"].browser.get("/conversations/api/people?q=c").json() == []
        mine = people["ada"].browser.get("/conversations/api/people?q=ada").json()
        assert mine == []  # never the caller

    def test_share_list_notices_and_audit(
        self, client: TestClient, people: dict[str, Person], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.identity import mail

        sent: list[dict[str, Any]] = []

        async def notice(secrets: Any, to: str, **kwargs: Any) -> bool:
            sent.append({"to": to, **kwargs})
            return True

        monkeypatch.setattr(mail, "send_share_notice", notice)
        cid = new_conversation(people["ada"])
        assert share(people["ada"], cid, people["vic"], "viewer").json()["status"] == "added"
        assert [(n["to"], n["kind"], n["role"]) for n in sent] == [(VIC, "shared", "read-only")]
        assert sent[0]["link"].endswith(f"/chat/{cid}")

        listed = people["vic"].browser.get("/conversations/api/shared").json()
        assert listed["unseen"] == 1
        row = listed["conversations"][0]
        assert (row["id"], row["role"], row["is_new"], row["owner_name"]) == (cid, "viewer", 1, ADA)
        assert people["ada"].browser.get("/conversations/api/list").json()[0]["is_shared"] == 1
        assert people["vic"].browser.get(f"/chat/{cid}").status_code == 200
        assert people["vic"].browser.get("/conversations/api/shared").json()["unseen"] == 0

        changed = share(people["ada"], cid, people["vic"], "contributor").json()
        assert changed["status"] == "changed"
        assert share(people["ada"], cid, people["vic"], "contributor").json()["status"] == (
            "unchanged"
        )
        assert len(sent) == 1  # a role change mails nobody

        shared_rows = events(client, spark_audit.CONVERSATION_SHARED)
        changed_rows = events(client, spark_audit.CONVERSATION_ROLE_CHANGED)
        assert [(r.resource_id, r.details["role"]) for r in shared_rows] == [(str(cid), "viewer")]
        assert [r.details["previous_role"] for r in changed_rows] == ["viewer"]
        assert all(r.source_ip for r in shared_rows + changed_rows)

    def test_removal_and_leaving(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        vic, cora = people["vic"], people["cora"]
        assert (
            people["ada"]
            .browser.delete(f"/conversations/api/{shared}/members/{vic.guid}")
            .status_code
            == 200
        )
        assert vic.browser.get(f"/chat/{shared}/api/history").status_code == 404
        assert (
            cora.browser.delete(f"/conversations/api/{shared}/members/{cora.guid}").status_code
            == 200
        )
        assert cora.browser.get(f"/chat/{shared}/api/history").status_code == 404
        owner_leaves = people["ada"].browser.delete(
            f"/conversations/api/{shared}/members/{people['ada'].guid}"
        )
        assert owner_leaves.status_code == 403
        rows = events(client, spark_audit.CONVERSATION_MEMBER_REMOVED)
        assert sorted((r.details["role"], r.details["left"]) for r in rows) == [
            ("contributor", True),
            ("viewer", False),
        ]

    def test_transfer(
        self, client: TestClient, people: dict[str, Person], shared: int, monkeypatch: Any
    ) -> None:
        from spark.identity import mail

        sent: list[str] = []

        async def notice(secrets: Any, to: str, **kwargs: Any) -> bool:
            sent.append(f"{kwargs['kind']}:{to}")
            return True

        monkeypatch.setattr(mail, "send_share_notice", notice)
        ada, cora = people["ada"], people["cora"]
        moved = ada.browser.post(
            f"/conversations/api/{shared}/transfer", json={"user_guid": cora.guid}
        )
        assert moved.status_code == 200, moved.text
        assert sent == [f"transferred:{CORA}"]
        assert (
            ada.browser.post(
                f"/conversations/api/{shared}/rename", json={"name": "Mine again"}
            ).status_code
            == 403
        )
        assert (
            cora.browser.post(
                f"/conversations/api/{shared}/rename", json={"name": "Cora's now"}
            ).status_code
            == 200
        )
        assert (
            ada.browser.post(f"/chat/{shared}/api/send", json={"message": "still here"}).status_code
            == 200
        )
        assert ada.browser.get(f"/chat/{shared}/api/info").json()["my_role"] == "contributor"
        rows = events(client, spark_audit.CONVERSATION_TRANSFERRED)
        assert [r.details["new_owner"] for r in rows] == [cora.guid]

    def test_transfer_to_someone_new(self, people: dict[str, Person], shared: int) -> None:
        moved = people["ada"].browser.post(
            f"/conversations/api/{shared}/transfer", json={"user_id": people["sam"].warden_id}
        )
        assert moved.status_code == 200, moved.text
        info = people["sam"].browser.get(f"/chat/{shared}/api/info").json()
        assert info["my_role"] == "owner"

    def test_debates_cannot_be_shared(self, people: dict[str, Person]) -> None:
        cid = new_conversation(people["ada"], "Debate")
        _call(
            people["ada"].browser,
            conversations.update_conversation,
            cid,
            people["ada"].guid,
            conversation_type="debate",
        )
        assert share(people["ada"], cid, people["cora"], "viewer").status_code == 400


class TestTurns:
    @pytest.mark.parametrize("path", ["api", "stream"])
    def test_the_senders_roles_and_permissions_reach_the_turn(
        self,
        client: TestClient,
        people: dict[str, Person],
        shared: int,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
    ) -> None:
        """A contributor's turn gets their own roles and permissions, never the owner's:
        the tools and MCP servers it may use (granted by role) are theirs."""
        from spark.identity.permissions import ROLES

        mgr = client.app.state.conversation_manager  # type: ignore[attr-defined]
        seen: dict[str, Any] = {}

        def send(conversation_id: int, message: str, user_guid: str, **kwargs: Any) -> dict:
            seen.update(kwargs, user_guid=user_guid)
            return {"content": "x", "status": "completed"}

        monkeypatch.setattr(mgr, "send_message", send)
        cora = people["cora"]
        if path == "api":
            assert (
                cora.browser.post(f"/chat/{shared}/api/send", json={"message": "m"}).status_code
                == 200
            )
        else:
            assert (
                "event: complete"
                in cora.browser.get(f"/stream/chat?message=m&conversation_id={shared}").text
            )
        assert seen["user_guid"] == cora.guid
        assert seen["roles"] == frozenset({USER})
        assert frozenset(seen["permissions"]) == ROLES[USER].permissions
        assert frozenset(seen["permissions"]) != ROLES[POWER_USER].permissions

    def test_the_turn_is_the_senders(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        cora = people["cora"]
        assert (
            cora.browser.post(f"/chat/{shared}/api/send", json={"message": "mine"}).status_code
            == 200
        )
        rows = _call(client, messages.get_messages, cora.guid, shared)
        assert [(m["role"], m["user_guid"]) for m in rows[-2:]] == [
            ("user", cora.guid),
            ("assistant", cora.guid),
        ]
        charged = client.app.state.sync_db.fetch_all(  # type: ignore[attr-defined]
            "SELECT DISTINCT user_guid FROM usage_tracking WHERE conversation_id = ?", (shared,)
        )
        assert {r["user_guid"] for r in charged} == {people["ada"].guid, cora.guid}
        history = people["vic"].browser.get(f"/chat/{shared}/api/history").json()
        assert [(m["role"], m["sender"]["name"]) for m in history[-2:]] == [
            ("user", CORA),
            ("assistant", CORA),
        ]
        assert history[-1]["sender"]["initials"] == "C"

    def test_a_model_the_sender_may_not_use(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        # The connection is the owner's role's only: Cora (a user) may not send with it.
        install_runtime(client.app, Script(), (POWER_USER,))
        info = people["cora"].browser.get(f"/chat/{shared}/api/info").json()
        assert info["may_send"] is False and "cannot use this conversation's model" in (
            info["send_refusal"]
        )
        refused = people["cora"].browser.post(f"/chat/{shared}/api/send", json={"message": "x"})
        assert refused.status_code == 403
        stream = people["cora"].browser.get(f"/stream/chat?message=x&conversation_id={shared}")
        assert stream.status_code == 403
        reasons = [
            r.details["reason"] for r in events(client, spark_audit.CONVERSATION_SEND_REFUSED)
        ]
        assert reasons == ["model", "model"]
        assert (
            people["ada"]
            .browser.post(f"/chat/{shared}/api/send", json={"message": "y"})
            .status_code
            == 200
        )

    def test_one_turn_at_a_time_with_two_senders(
        self, client: TestClient, people: dict[str, Person], shared: int, script: Script
    ) -> None:
        script.hold.clear()
        barrier = threading.Barrier(2, timeout=30)
        answers: dict[str, Any] = {}

        def send(who: str) -> None:
            barrier.wait()
            answers[who] = people[who].browser.post(
                f"/chat/{shared}/api/send", json={"message": f"from {who}"}
            )

        threads = [threading.Thread(target=send, args=(w,)) for w in ("ada", "cora")]
        for t in threads:
            t.start()
        try:
            wait_for(lambda: len(answers) == 1)  # the loser is answered at once
        finally:
            script.hold.set()  # never leave a turn held, even when the check fails
            for t in threads:
                t.join(60)
        statuses = sorted(a.status_code for a in answers.values())
        assert statuses == [200, 409]
        busy = next(a for a in answers.values() if a.status_code == 409).json()
        assert busy["error"].startswith("Waiting for ") and busy["error"].endswith(
            "'s turn to finish"
        )
        assert turn_row(client, shared) is None  # released on completion
        reasons = [
            r.details["reason"] for r in events(client, spark_audit.CONVERSATION_SEND_REFUSED)
        ]
        assert reasons == ["turn"]

    def test_the_lock_is_released_on_failure(
        self, client: TestClient, people: dict[str, Person], shared: int, monkeypatch: Any
    ) -> None:
        mgr = client.app.state.conversation_manager  # type: ignore[attr-defined]

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the model broke")

        monkeypatch.setattr(mgr, "send_message", fail)
        assert (
            people["cora"]
            .browser.post(f"/chat/{shared}/api/send", json={"message": "x"})
            .status_code
            == 500
        )
        wait_for(lambda: turn_row(client, shared) is None)
        stream = people["cora"].browser.get(f"/stream/chat?message=x&conversation_id={shared}")
        assert "event: error" in stream.text
        wait_for(lambda: turn_row(client, shared) is None)

    def test_a_viewer_follows_a_turn_and_removal_cuts_the_view(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        from spark.web.live import bus_for

        bus = bus_for(client.app)
        seen: dict[str, str] = {}

        def watch() -> None:
            seen["text"] = (
                people["vic"].browser.get(f"/stream/conversation?conversation_id={shared}").text
            )

        watcher = threading.Thread(target=watch)
        watcher.start()
        wait_for(lambda: bus.watchers(shared) == 1)
        wait_for(lambda: bus.listening.is_set())
        assert (
            people["cora"]
            .browser.post(f"/chat/{shared}/api/send", json={"message": "x"})
            .status_code
            == 200
        )
        removed = people["ada"].browser.delete(
            f"/conversations/api/{shared}/members/{people['vic'].guid}"
        )
        assert removed.status_code == 200
        watcher.join(30)
        assert not watcher.is_alive()
        order = [
            line[len("event: ") :]
            for line in seen["text"].splitlines()
            if line.startswith("event: ")
        ]
        assert order[0] == "ready" and order[-1] == "removed"
        assert order.index("turn_started") < order.index("turn_finished")
        assert f'"name": "{CORA}"' in seen["text"]
        # Gone at once: the next look is refused.
        again = people["vic"].browser.get(f"/stream/conversation?conversation_id={shared}")
        assert again.status_code == 404

    def test_removing_the_sender_cancels_their_turn(
        self, client: TestClient, people: dict[str, Person], shared: int, script: Script
    ) -> None:
        script.hold.clear()
        script.waiting.clear()
        answer: dict[str, Any] = {}

        def send() -> None:
            answer["resp"] = people["cora"].browser.post(
                f"/chat/{shared}/api/send", json={"message": "long one"}
            )

        sender = threading.Thread(target=send)
        sender.start()
        assert script.waiting.wait(30)
        assert turn_row(client, shared)["user_guid"] == people["cora"].guid
        removed = people["ada"].browser.delete(
            f"/conversations/api/{shared}/members/{people['cora'].guid}"
        )
        assert removed.status_code == 200
        assert turn_row(client, shared) is None  # the lease went with the member
        script.hold.set()
        sender.join(60)
        assert answer["resp"].json()["status"] == "cancelled"


class TestPrivacy:
    def test_links_graphs_and_files_stay_the_owners(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        ada, cora = people["ada"], people["cora"]
        mine = new_conversation(cora, "Cora's own")
        assert (
            cora.browser.post(f"/chat/{shared}/api/links", json={"target_id": mine}).status_code
            == 403
        )
        other = new_conversation(ada, "Ada's other")
        assert (
            ada.browser.post(f"/chat/{shared}/api/links", json={"target_id": other}).status_code
            == 200
        )
        # A conversation shared with the owner is not linkable either.
        theirs = new_conversation(cora, "Cora's shared")
        share(cora, theirs, ada, "contributor")
        assert (
            ada.browser.post(f"/chat/{shared}/api/links", json={"target_id": theirs}).status_code
            == 404
        )
        assert cora.browser.post(
            "/knowledge/api/build", json={"scope": f"conv:{shared}"}
        ).status_code in (403, 404)

        private = ada.browser.post(
            "/files/api", files={"file": ("p.txt", b"private", "text/plain")}
        )
        attached = cora.browser.post(
            "/files/api",
            files={"file": ("a.txt", b"for everyone", "text/plain")},
            data={"conversation_id": str(shared)},
        )
        assert attached.status_code == 201, attached.text
        refused = people["vic"].browser.post(
            "/files/api",
            files={"file": ("v.txt", b"no", "text/plain")},
            data={"conversation_id": str(shared)},
        )
        assert refused.status_code == 403
        private_id = private.json()["file"]["id"]
        attached_id = attached.json()["file"]["id"]
        assert people["vic"].browser.get(f"/files/api/{attached_id}").content == b"for everyone"
        assert ada.browser.get(f"/files/api/{attached_id}").status_code == 200
        assert cora.browser.get(f"/files/api/{private_id}").status_code == 404
        assert people["sam"].browser.get(f"/files/api/{attached_id}").status_code == 404

    def test_a_file_named_in_a_message_is_reachable(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        ada = people["ada"]
        upload = ada.browser.post("/files/api", files={"file": ("n.txt", b"notes", "text/plain")})
        fid = upload.json()["file"]["id"]
        assert people["vic"].browser.get(f"/files/api/{fid}").status_code == 404
        sent = ada.browser.post(
            f"/chat/{shared}/api/send", json={"message": "see attached", "attachments": [fid]}
        )
        assert sent.status_code == 200, sent.text
        assert people["vic"].browser.get(f"/files/api/{fid}").content == b"notes"

    def test_export_by_a_viewer(
        self, client: TestClient, people: dict[str, Person], shared: int
    ) -> None:
        exported = people["vic"].browser.get(f"/chat/{shared}/api/export?format=markdown")
        assert exported.status_code == 200 and "hello all" in exported.text
        rows = events(client, spark_audit.EXPORT_DOWNLOADED)
        assert [r.details.get("user_guid") or r.actor for r in rows]


class TestTwoReplicas:
    def test_the_lock_and_live_views_hold_across_replicas(
        self,
        client: TestClient,
        people: dict[str, Person],
        shared: int,
        script: Script,
        server_db: DatabaseSettings,
        tmp_path: Path,
    ) -> None:
        config = write_config(tmp_path / "replica.yaml", autonomous_actions={"enabled": True})
        with server_client(config, server_db) as replica:
            install_runtime(replica.app, script, ("user", "power_user"))
            cora = another_client(replica)
            vic = another_client(replica)
            assert sign_in(cora, CORA).status_code == 303
            assert sign_in(vic, VIC).status_code == 303
            from spark.web.live import bus_for

            seen: dict[str, str] = {}

            def watch() -> None:
                seen["text"] = vic.get(f"/stream/conversation?conversation_id={shared}").text

            watcher = threading.Thread(target=watch)
            watcher.start()
            remote = bus_for(replica.app)
            wait_for(lambda: remote.watchers(shared) == 1 and remote.listening.is_set())

            # Ada's long turn runs on the first replica...
            script.hold.clear()
            script.waiting.clear()
            answer: dict[str, Any] = {}

            def send() -> None:
                answer["resp"] = people["ada"].browser.post(
                    f"/chat/{shared}/api/send", json={"message": "on replica one"}
                )

            sender = threading.Thread(target=send)
            sender.start()
            assert script.waiting.wait(30)
            # ...so Cora's send on the second waits for it.
            busy = cora.post(f"/chat/{shared}/api/send", json={"message": "on replica two"})
            assert busy.status_code == 409
            assert busy.json()["holder"]["name"] == ADA
            script.hold.set()
            sender.join(60)
            assert answer["resp"].status_code == 200
            assert cora.post(f"/chat/{shared}/api/send", json={"message": "now"}).status_code == 200

            # Vic, watching on the second replica, is cut off by a removal on the first.
            removed = people["ada"].browser.delete(
                f"/conversations/api/{shared}/members/{people['vic'].guid}"
            )
            assert removed.status_code == 200
            watcher.join(30)
            assert not watcher.is_alive()
            text = seen["text"]
            assert text.count("event: turn_started") >= 2  # one from each replica
            assert f'"name": "{ADA}"' in text and f'"name": "{CORA}"' in text
            assert text.rstrip().splitlines()[-2:][0] == "event: removed"
