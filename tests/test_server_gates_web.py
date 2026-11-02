"""Server mode: pending interactions belong to their user, and role gates on actions and skills.

The real app runs on a PostgreSQL clone at head with synthetic users signed in
through warden (the fixtures of ``tests.test_user_scoping_web``).

* A chat stream's pending tool permission, agent model approval, agent cancel
  token and the stream itself are bound to the user whose turn raised them:
  another user's id answers 404 and changes nothing.
* Creating, editing, toggling, running now and AI-creating actions need
  ``spark.actions`` (a user is refused with 403, a power user is not); listing
  one's own actions stays open.
* Importing, deleting and reloading skills (shared by every user) need
  ``spark.settings.instance``; turning a skill on or off for oneself stays open.
"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.database import autonomous_actions, conversations
from spark.identity.permissions import ADMINISTRATOR, POWER_USER, USER
from tests.test_user_scoping_web import (  # noqa: F401 - fixtures
    _call,
    _signed_in,
    client,
    host,
    server_db,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
ROOT = "root@example.com"


def _wait_for(check: Callable[[], Any], seconds: float = 20.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


class _PromptingManager:
    """A conversation manager whose turn raises every kind of pending interaction, then waits."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}

    def send_message(
        self,
        conversation_id: int,
        message: str,
        user_guid: str,
        *,
        cancel_token: Any = None,
        callbacks: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        agent = callbacks.agent_cancel_register("agent-ada")
        self.answers["permission"] = callbacks.tool_permission("web_search", {})
        self.answers["model"] = callbacks.agent_model("helper", "task", "model-a", [], "")
        _wait_for(lambda: agent.is_cancelled())
        self.answers["turn_cancelled_with_agent"] = cancel_token.is_cancelled()
        _wait_for(lambda: cancel_token.is_cancelled())
        return {"status": "cancelled", "content": ""}


class TestPendingInteractions:
    def test_another_users_pending_ids_are_404_and_left_alone(self, client: TestClient) -> None:
        ada, ada_guid = _signed_in(client, ADA)
        grace, _ = _signed_in(client, GRACE)
        cid = _call(client, conversations.create_conversation, "Ada's", "model-a", ada_guid)
        manager = _PromptingManager()
        state = client.app.state  # type: ignore[attr-defined]
        state.conversation_manager = manager

        streamed: list[Any] = []
        stream = threading.Thread(
            target=lambda: streamed.append(
                ada.get(f"/stream/chat?conversation_id={cid}&message=hello")
            ),
            daemon=True,
        )
        stream.start()

        # The tool permission prompt: Grace cannot answer it, Ada can.
        permission_id = _wait_for(lambda: next(iter(getattr(state, "permission_events", {})), None))
        body = {"request_id": permission_id, "decision": "allowed"}
        assert grace.post("/chat/permission/respond", json=body).status_code == 404
        assert not state.permission_events[permission_id].is_set()
        assert ada.post("/chat/permission/respond", json=body).status_code == 200
        _wait_for(lambda: "permission" in manager.answers)
        assert manager.answers["permission"] == "allowed"

        # The agent model approval.
        approval_id = _wait_for(lambda: next(iter(getattr(state, "agent_model_events", {})), None))
        body = {"request_id": approval_id, "model_id": "model-b"}
        assert grace.post("/chat/agent/model-approve", json=body).status_code == 404
        assert not state.agent_model_events[approval_id].is_set()
        assert ada.post("/chat/agent/model-approve", json=body).status_code == 200
        _wait_for(lambda: "model" in manager.answers)
        assert manager.answers["model"] == "model-b"

        # The agent's and the stream's cancel tokens.
        stream_id = next(iter(state.turn_cancel_tokens))
        agent = state.agent_cancel_tokens["agent-ada"]
        turn = state.turn_cancel_tokens[stream_id]
        assert grace.post("/chat/agent/cancel", json={"agent_id": "agent-ada"}).status_code == 404
        assert grace.post("/stream/cancel", json={"stream_id": stream_id}).status_code == 404
        assert not agent.is_cancelled() and not turn.is_cancelled()
        assert ada.post("/chat/agent/cancel", json={"agent_id": "agent-ada"}).status_code == 200
        _wait_for(lambda: "turn_cancelled_with_agent" in manager.answers)
        assert manager.answers["turn_cancelled_with_agent"] is False
        assert ada.post("/stream/cancel", json={"stream_id": stream_id}).status_code == 200
        stream.join(30)
        assert not stream.is_alive()
        assert streamed[0].status_code == 200
        assert "cancelled" in streamed[0].text

        # Once the turn is over nothing of it is addressable, by anyone.
        assert ada.post("/stream/cancel", json={"stream_id": stream_id}).status_code == 404
        assert state.interaction_owners == {}

    def test_unknown_ids_are_404_on_a_server(self, client: TestClient) -> None:
        grace, _ = _signed_in(client, GRACE)
        answers = [
            grace.post("/chat/permission/respond", json={"request_id": "nope", "decision": "x"}),
            grace.post("/chat/agent/model-approve", json={"request_id": "nope", "model_id": "m"}),
            grace.post("/chat/agent/cancel", json={"agent_id": "nope"}),
            grace.post("/stream/cancel", json={"stream_id": "nope"}),
        ]
        assert [r.status_code for r in answers] == [404] * 4


def _action(client: TestClient, guid: str, name: str) -> int:
    aid: int = _call(client, autonomous_actions.create_action, name, "p", "model-a", guid)
    return aid


class TestActionsNeedTheActionsPermission:
    def test_a_user_may_list_but_not_create_change_or_run(self, client: TestClient) -> None:
        grace, guid = _signed_in(client, GRACE, USER)
        aid = _action(client, guid, "grace-old")
        assert grace.get("/actions/api/list").status_code == 200
        body = {"name": "grace-new", "prompt": "p", "model_id": "model-a"}
        refused = [
            grace.post("/actions/api/create", json=body),
            grace.put(f"/actions/api/{aid}", json={"name": "renamed"}),
            grace.post(f"/actions/api/{aid}/toggle", json={"enabled": False}),
            grace.post(f"/actions/api/{aid}/run-now"),
            grace.post("/actions/api/ai-create", json={"message": "make one"}),
        ]
        assert [r.status_code for r in refused] == [403] * len(refused), [
            (r.request.url.path, r.status_code) for r in refused
        ]
        [row] = _call(client, autonomous_actions.get_all_actions, guid)
        assert (row["name"], row["is_enabled"]) == ("grace-old", 1)

    def test_a_power_user_may(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        ran: list[int] = []

        class Executor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def execute(self, action_id: int) -> None:
                ran.append(action_id)

        monkeypatch.setattr("spark.scheduler.executor.ActionExecutor", Executor)
        ada, guid = _signed_in(client, ADA, POWER_USER)
        created = ada.post(
            "/actions/api/create", json={"name": "ada-new", "prompt": "p", "model_id": "model-a"}
        )
        assert created.status_code == 200, created.text
        aid = created.json()["id"]
        assert ada.put(f"/actions/api/{aid}", json={"name": "ada-renamed"}).status_code == 200
        assert ada.post(f"/actions/api/{aid}/toggle", json={"enabled": False}).status_code == 200
        assert ada.post(f"/actions/api/{aid}/run-now").status_code == 200
        _wait_for(lambda: ran)
        assert ran == [aid]
        # ai-create passes the gate (no model service in this app: 503, never 403).
        assert ada.post("/actions/api/ai-create", json={"message": "x"}).status_code == 503


def _skill_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: d\n---\nbody\n")
    return buf.getvalue()


@pytest.fixture
def skills(client: TestClient, tmp_path: Path) -> Any:
    """The app's skills in the test's own directory."""
    from spark.skills.manager import SkillsManager

    manager = SkillsManager(tmp_path / "skills")
    client.app.state.skills_manager = manager  # type: ignore[attr-defined]
    return manager


@pytest.mark.usefixtures("skills")
class TestSkillsNeedTheInstanceSettingsPermission:
    def test_a_user_may_toggle_but_not_import_delete_or_reload(
        self, client: TestClient, skills: Any
    ) -> None:
        grace, _ = _signed_in(client, GRACE, POWER_USER)
        refused = [
            grace.post(
                "/skills/api/import",
                files={"file": ("s.zip", _skill_zip("probe-skill"), "application/zip")},
            ),
            grace.post("/skills/api/delete", json={"name": "probe-skill"}),
            grace.post("/skills/api/reload"),
        ]
        assert [r.status_code for r in refused] == [403] * len(refused)
        assert not (skills.user_dir / "probe-skill").exists()
        toggled = grace.post("/skills/api/toggle", json={"name": "probe-skill", "enabled": False})
        assert toggled.status_code == 200

    def test_an_administrator_passes_the_gate(self, client: TestClient) -> None:
        root, _ = _signed_in(client, ROOT, ADMINISTRATOR)
        assert root.post("/skills/api/reload").status_code == 200
        assert root.post("/skills/api/delete", json={"name": "no-such-skill"}).status_code == 404
        bad = root.post(
            "/skills/api/import", files={"file": ("s.zip", b"not a zip", "application/zip")}
        )
        assert bad.status_code == 400
