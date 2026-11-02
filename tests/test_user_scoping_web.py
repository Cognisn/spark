"""Server mode: one signed-in user can never reach another user's data through the web.

The real app runs on a PostgreSQL clone at head with two synthetic users signed
in through warden. Ada's data is created first; Grace then lists, reads, changes,
deletes and runs it by id. Every id of Ada's answers Grace with 404 (never 403,
which would reveal that it exists), and Ada's data is unchanged afterwards.

The host-process endpoints (quit, open folder, the settings lock) are refused on
a server, and the MCP server settings need spark.settings.instance.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import (
    agents,
    autonomous_actions,
    conversation_links,
    conversations,
    debates,
    mcp_ops,
    memories,
    messages,
)
from spark.identity.permissions import ADMINISTRATOR
from spark.knowledge import store
from spark.mode import DatabaseSettings, Dialect, Mode
from tests.identity_web_helpers import (
    another_client,
    make_user,
    server_client,
    sign_in,
    write_config,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
ROOT = "root@example.com"
FETCH = {"Accept": "application/json"}
EMB = np.ones(8, dtype=np.float32)


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def server_db(pg_server: str, _pg_template: str) -> Iterator[DatabaseSettings]:
    base, _, _ = pg_server.rpartition("/")
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{_pg_template}"')
    try:
        yield DatabaseSettings(Dialect.POSTGRESQL, url=f"{base}/{name}")
    finally:
        with psycopg.connect(pg_server, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    """Record (never perform) what the host-process endpoints would do; config.yaml
    writes land in the test's own directory."""
    import spark.core.application as application

    monkeypatch.setattr(application, "_get_config_path", lambda: tmp_path / "host-config.yaml")
    done: list[Any] = []
    monkeypatch.setattr(os, "kill", lambda *args: done.append(("kill", args)))

    async def spawn(*args: Any, **_: Any) -> None:
        done.append(("spawn", args))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return done


@pytest.fixture
def client(tmp_path: Path, server_db: DatabaseSettings, host: list[Any]) -> Iterator[TestClient]:
    files_pkg.configure(
        _Settings({"files.root": str(tmp_path / "volume")}),
        data_dir=tmp_path,
        runtime_mode=Mode.SERVER,
    )
    config = write_config(tmp_path / "config.yaml", autonomous_actions={"enabled": True})
    try:
        with server_client(config, server_db) as test_client:
            from spark.core.conversation_manager import ConversationManager
            from spark.llm.context_limits import ContextLimitResolver

            app = test_client.app
            app.state.conversation_manager = ConversationManager(  # type: ignore[attr-defined]
                app.state.sync_db,  # type: ignore[attr-defined]
                MagicMock(),
                ContextLimitResolver(None),
                user_guid="process-guid",
            )
            yield test_client
    finally:
        files_pkg.reset()


def _signed_in(client: TestClient, email: str, *roles: str) -> tuple[TestClient, str]:
    make_user(client, email, *roles)
    browser = another_client(client)
    assert sign_in(browser, email).status_code == 303
    guid = browser.get("/auth/session", headers=FETCH).json()["user_guid"]
    return browser, str(guid)


@pytest.fixture
def users(client: TestClient) -> dict[str, Any]:
    ada, ada_guid = _signed_in(client, ADA)
    grace, grace_guid = _signed_in(client, GRACE)
    return {"ada": ada, "ada_guid": ada_guid, "grace": grace, "grace_guid": grace_guid}


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


@pytest.fixture
def adas(client: TestClient, users: dict[str, Any]) -> dict[str, Any]:
    """Ada's data: conversations, messages, tools, a debate, a panel, an action, a memory,
    a file, a knowledge graph node and a link."""
    ada = users["ada_guid"]
    cid = _call(client, conversations.create_conversation, "Ada's notes", "model-a", ada)
    other = _call(client, conversations.create_conversation, "Ada's other", "model-a", ada)
    _call(client, messages.add_message, cid, "user", "Ada's secret plan", 5, ada)
    _call(client, mcp_ops.record_transaction, cid, "web_search", "{}", "found", ada)
    _call(client, mcp_ops.set_embedded_tool_enabled, cid, "web_search", False, ada)
    _call(client, agents.create_agent_run, "agent-ada", cid, "R", "t", "orchestrator", "m", ada)
    _call(client, conversation_links.add_link, cid, other, ada)

    debate = _call(client, conversations.create_conversation, "Ada's debate", "m", ada)
    _call(client, conversations.update_conversation, debate, ada, conversation_type="debate")
    agents_spec = {r: {"model_id": "m"} for r in ("pro", "con", "judge")}
    _call(client, debates.create_debate, debate, "Tea", "fixed", 1, ada, agents_spec)
    _call(client, debates.add_turn, ada, debate, 1, "pro", "argument", "tea is best")

    panel = _call(client, conversations.create_conversation, "Ada's panel", "m", ada)
    _call(client, conversations.update_conversation, panel, ada, conversation_type="panel")
    panel_spec = {"moderator": {"model_id": "m"}, "panellist:1": {"model_id": "m"}}
    _call(client, debates.create_debate, panel, "Coffee", "fixed", 1, ada, panel_spec)

    action = _call(client, autonomous_actions.create_action, "ada-nightly", "p", "m", ada)
    run = _call(client, autonomous_actions.start_run, action, ada)
    _call(client, autonomous_actions.complete_run, ada, run, result_text="done")
    _call(client, autonomous_actions.set_tool_permission, action, "web_search", "denied", ada)

    memory = _call(client, memories.add_memory, ada, "Ada likes tea", "facts", EMB)
    _call(client, store.upsert_node, f"conv:{cid}", "Tea", "thing", "d", None, ada)
    _call(client, store.upsert_node, "global", "Coffee", "thing", "d", None, ada)

    upload = users["ada"].post(
        "/files/api", files={"file": ("plan.txt", b"Ada's plan", "text/plain")}
    )
    assert upload.status_code == 201, upload.text
    return {
        "cid": cid,
        "other": other,
        "debate": debate,
        "panel": panel,
        "action": action,
        "memory": memory,
        "file": upload.json()["file"]["id"],
    }


def _grace_conversation(client: TestClient, users: dict[str, Any]) -> int:
    cid: int = _call(
        client, conversations.create_conversation, "Grace's", "model-a", users["grace_guid"]
    )
    return cid


class TestConversationsAndChat:
    def test_every_conversation_route_is_404_for_another_user(
        self, client: TestClient, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        cid = adas["cid"]
        mine = _grace_conversation(client, users)
        refused = [
            grace.delete(f"/conversations/api/{cid}"),
            grace.post(f"/conversations/api/{cid}/rename", json={"name": "taken"}),
            grace.post(f"/conversations/api/{cid}/model", json={"model_id": "other"}),
            grace.post(f"/conversations/api/{cid}/favourite", json={"favourite": True}),
            grace.get(f"/chat/{cid}/api/history"),
            grace.get(f"/chat/{cid}/api/tool-activity"),
            grace.get(f"/chat/{cid}/api/agent-history"),
            grace.post(f"/chat/{cid}/api/send", json={"message": "hello"}),
            grace.get(f"/chat/{cid}/api/info"),
            grace.post(f"/chat/{cid}/api/settings", json={"instructions": "leak"}),
            grace.get(f"/chat/{cid}/api/tools"),
            grace.post(
                f"/chat/{cid}/api/tools",
                json={"type": "embedded", "name": "web_search", "enabled": True},
            ),
            grace.post(
                f"/chat/{cid}/api/tools", json={"type": "skill", "name": "s", "enabled": False}
            ),
            grace.get(f"/chat/{cid}/api/links"),
            grace.post(f"/chat/{cid}/api/links", json={"target_id": mine}),
            grace.post(f"/chat/{mine}/api/links", json={"target_id": cid}),
            grace.delete(f"/chat/{cid}/api/links/{adas['other']}"),
            grace.get(f"/chat/{cid}/api/export?format=json"),
            grace.get(f"/stream/chat?conversation_id={cid}&message=hello"),
        ]
        assert [r.status_code for r in refused] == [404] * len(refused), [
            (r.request.method, r.request.url.path, r.status_code) for r in refused
        ]
        assert grace.get("/conversations/api/search?q=Ada").json() == []
        assert [c["id"] for c in grace.get("/conversations/api/list").json()] == [mine]

        ada = users["ada"]
        info = ada.get(f"/chat/{cid}/api/info").json()
        assert (info["name"], info["model_id"], info["instructions"]) == (
            "Ada's notes",
            "model-a",
            None,
        )
        assert [m["content"] for m in ada.get(f"/chat/{cid}/api/history").json()] == [
            "Ada's secret plan"
        ]
        assert len(ada.get(f"/chat/{cid}/api/tool-activity").json()) == 1
        assert len(ada.get(f"/chat/{cid}/api/agent-history").json()) == 1
        tools = ada.get(f"/chat/{cid}/api/tools").json()["embedded"]
        assert {t["name"]: t["enabled"] for t in tools}.get("web_search", False) is False
        assert [c["id"] for c in ada.get(f"/chat/{cid}/api/links").json()["linked"]] == [
            adas["other"]
        ]
        assert ada.get(f"/chat/{cid}/api/export?format=json").status_code == 200
        assert _call(client, conversation_links.get_links, mine, users["grace_guid"]) == []


class TestDebatesAndPanels:
    def test_debate_and_panel_routes_are_404_for_another_user(
        self, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        debate, panel = adas["debate"], adas["panel"]
        refused = [
            grace.get(f"/debate/api/state?conversation_id={debate}"),
            grace.post("/debate/api/prompt", json={"conversation_id": debate, "message": "x"}),
            grace.get(f"/stream/debate?conversation_id={debate}"),
            grace.get(f"/panel/api/state?conversation_id={panel}"),
            grace.post("/panel/api/prompt", json={"conversation_id": panel, "message": "x"}),
            grace.get(f"/stream/panel?conversation_id={panel}"),
            grace.get(f"/chat/{debate}/api/export?format=markdown"),
        ]
        assert [r.status_code for r in refused] == [404] * len(refused), [
            (r.request.url.path, r.status_code) for r in refused
        ]
        state = users["ada"].get(f"/debate/api/state?conversation_id={debate}").json()
        assert [t["content"] for t in state["turns"]] == ["tea is best"]


class TestActions:
    def test_action_routes_are_404_for_another_user(
        self, client: TestClient, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        aid = adas["action"]
        assert grace.get("/actions/api/list").json() == []
        refused = [
            grace.put(f"/actions/api/{aid}", json={"name": "taken"}),
            grace.post(f"/actions/api/{aid}/toggle", json={"enabled": False}),
            grace.get(f"/actions/api/{aid}/tools"),
            grace.post(f"/actions/api/{aid}/tools", json={"name": "web_search", "enabled": True}),
            grace.get(f"/actions/api/{aid}/runs"),
            grace.post(f"/actions/api/{aid}/run-now"),
            grace.delete(f"/actions/api/{aid}"),
        ]
        assert [r.status_code for r in refused] == [404] * len(refused), [
            (r.request.method, r.request.url.path, r.status_code) for r in refused
        ]
        ada = users["ada"]
        listed = ada.get("/actions/api/list").json()
        assert [(a["name"], a["is_enabled"]) for a in listed] == [("ada-nightly", 1)]
        assert len(ada.get(f"/actions/api/{aid}/runs").json()) == 1
        perms = _call(client, autonomous_actions.get_tool_permissions, users["ada_guid"], aid)
        assert perms == {"web_search": "denied"}

    def test_action_names_are_per_user(
        self, client: TestClient, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        """Another user's action name is free, and a clash names only the caller's own action."""
        grace = users["grace"]
        body = {"name": "ada-nightly", "prompt": "p", "model_id": "m"}
        created = grace.post("/actions/api/create", json=body)
        assert created.status_code == 200, created.text
        again = grace.post("/actions/api/create", json=body)
        assert again.status_code == 409
        assert again.json() == {"error": "You already have an action named 'ada-nightly'."}
        other = grace.post("/actions/api/create", json={**body, "name": "grace-daily"}).json()
        renamed = grace.put(f"/actions/api/{other['id']}", json={"name": "ada-nightly"})
        assert renamed.status_code == 409
        assert renamed.json() == {"error": "You already have an action named 'ada-nightly'."}
        assert users["ada_guid"] not in renamed.text
        names = sorted(a["name"] for a in grace.get("/actions/api/list").json())
        assert names == ["ada-nightly", "grace-daily"]
        ada = [a["name"] for a in users["ada"].get("/actions/api/list").json()]
        assert ada == ["ada-nightly"]


class TestMemoriesFilesKnowledge:
    def test_memories_are_404_for_another_user(
        self, client: TestClient, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        mid = adas["memory"]
        assert grace.delete(f"/memories/api/{mid}").status_code == 404
        assert grace.put(f"/memories/api/{mid}", json={"importance": 0.9}).status_code == 404
        exported = grace.get("/memories/api/export").json()
        assert exported["memories"] == []
        row = _call(client, memories.get_memory, mid, users["ada_guid"])
        assert row is not None and row["importance"] == 0.5

    def test_files_are_404_for_another_user(
        self, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        fid = adas["file"]
        assert grace.get(f"/files/api/{fid}").status_code == 404
        assert grace.delete(f"/files/api/{fid}").status_code == 404
        assert grace.get("/files/api").json()["files"] == []
        assert users["ada"].get(f"/files/api/{fid}").content == b"Ada's plan"

    def test_knowledge_is_404_for_another_users_conversation(
        self, client: TestClient, users: dict[str, Any], adas: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        scope = f"conv:{adas['cid']}"
        refused = [
            grace.get(f"/knowledge/api/graph?scope={scope}"),
            grace.get(f"/knowledge/api/search?q=tea&scope={scope}"),
            grace.post("/knowledge/api/build", json={"scope": scope}),
            grace.post("/knowledge/api/clear", json={"scope": scope}),
            grace.post("/knowledge/api/cancel", json={"scope": scope}),
        ]
        assert [r.status_code for r in refused] == [404] * len(refused), [
            (r.request.url.path, r.status_code) for r in refused
        ]
        assert grace.get("/knowledge/api/graph?scope=global").json()["total_nodes"] == 0
        assert grace.post("/knowledge/api/clear", json={"scope": "global"}).status_code == 200
        ada = users["ada"]
        assert ada.get(f"/knowledge/api/graph?scope={scope}").json()["total_nodes"] == 1
        assert ada.get("/knowledge/api/graph?scope=global").json()["total_nodes"] == 1

    def test_a_build_of_one_user_is_not_another_users_to_cancel(
        self, client: TestClient, users: dict[str, Any]
    ) -> None:
        from spark.core.cancellation import CancellationToken
        from spark.web.endpoints.knowledge import build_key

        token = CancellationToken()
        tokens = client.app.state.kg_build_tokens = {}  # type: ignore[attr-defined]
        tokens[build_key(users["ada_guid"], "global")] = token
        resp = users["grace"].post("/knowledge/api/cancel", json={"scope": "global"})
        assert resp.json() == {"status": "not_building"}
        assert not token.is_cancelled()


class TestHostProcessEndpoints:
    def test_refused_on_a_server(self, users: dict[str, Any], host: list[Any]) -> None:
        grace = users["grace"]
        refused = [
            grace.post("/api/quit"),
            grace.post("/api/open-folder?type=logs"),
            grace.post("/settings/api/lock", json={"password": "dev-only-lock"}),
            grace.post("/settings/api/unlock", json={"password": "dev-only-lock"}),
        ]
        assert [r.status_code for r in refused] == [404] * len(refused)
        assert host == []

    def test_mcp_settings_need_the_instance_settings_permission(
        self, client: TestClient, users: dict[str, Any]
    ) -> None:
        grace = users["grace"]
        server = {"name": "probe", "transport": "stdio", "command": "echo"}
        refused = [
            grace.get("/settings/mcp/api/list"),
            grace.post("/settings/mcp/api/save", json=server),
            grace.post("/settings/mcp/api/toggle", json={"name": "probe", "enabled": False}),
            grace.delete("/settings/mcp/api/probe"),
            grace.get("/settings/mcp/api/probe/config"),
        ]
        assert [r.status_code for r in refused] == [403] * len(refused)
        root, _ = _signed_in(client, ROOT, ADMINISTRATOR)
        assert root.get("/settings/mcp/api/list").status_code == 200
