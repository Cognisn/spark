"""The instance setting ``tool_permissions.auto_approve`` ("Auto-approve Tools").

When it is on, a tool's first use in a conversation runs without the permission
dialogue, as if allowed for that call (nothing is stored, so switching it off asks
again). What still asks or refuses:

* a tool the user denied, for the conversation or globally, stays denied;
* ``run_command`` and ``send_email`` ask while their ``require_approval`` is on;
* skill authoring (``create_skill``, ``update_skill``) always asks.

The setting is read at each check through the settings accessor: ``config.yaml`` in
personal mode, the administrator's instance setting in server mode, so a change
applies on the next tool call without a restart. It applies to every user of the
instance, and only an administrator can change it in server mode.

Guards under test (each mutation-checked): the auto-approve branch itself, its
exclusion of the forced prompts, the deny that stays, the read at each check, and
failing closed when the setting cannot be read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark import mode
from spark.core.conversation_manager import ConversationManager, TurnCallbacks
from spark.database import tool_permissions
from spark.instance_settings import access, accessor, registry
from spark.mode import Mode, RuntimeSettings
from tests.model_helpers import FakeModels
from tests.test_conversation_manager import StubLLMService
from tests.test_instance_settings_web import (  # noqa: F401 - fixtures used by name
    Deny,
    _clean,
    config_file,
    personal,
    server,
    server_db,
    signed_in_as,
    store,
)

USER = "user-auto-approve-example"
KEY = "tool_permissions.auto_approve"
DENIED = "The user denied permission for this tool call"


class Source:
    """A settings source whose auto-approve value a test can change between calls."""

    def __init__(self, on: bool) -> None:
        self.on = on

    def get(self, key: str) -> Any:
        return self.on if key == KEY else registry.spec(key).default


class Broken:
    """A settings source that cannot be read."""

    def get(self, key: str) -> Any:
        raise RuntimeError("the settings store is unreachable")


class Harness:
    """One conversation on a ConversationManager whose tools only record that they ran."""

    def __init__(self, db: Any, config: dict[str, Any] | None = None, user: str = USER) -> None:
        self.db = db
        self.user = user
        self.manager = ConversationManager(
            db, FakeModels(StubLLMService()), embedded_tools_config=config or {}
        )
        self.cid = self.manager.create_conversation("Auto-approve", "stub-model", user)
        self.asked: list[str] = []
        self.ran: list[str] = []
        self.answer = "denied"

        def call_tool(name: str, tool_input: dict, **_: Any) -> tuple[str, bool]:
            self.ran.append(name)
            return f"{name} ran", False

        self.manager._call_tool = call_tool  # type: ignore[method-assign]

    def ask(self, name: str, tool_input: dict) -> str:
        self.asked.append(name)
        return self.answer

    def use(self, name: str, ask: Callable | None = None) -> dict:
        callbacks = TurnCallbacks(tool_permission=ask or self.ask)
        return self.manager._execute_single_tool(
            {"id": f"call-{len(self.asked)}-{len(self.ran)}", "name": name, "input": {}},
            self.cid,
            self.user,
            callbacks=callbacks,
        )

    def stored(self, name: str) -> bool | None:
        return self.db.call(tool_permissions.is_tool_allowed, self.user, self.cid, name)


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    accessor.reset()
    access.reset()
    mode.reset()


@pytest.fixture
def harness(sync_db: Any, sqlite_settings: Any) -> Harness:
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    return Harness(sync_db)


def _approval(command: bool = True, email: bool = True) -> dict[str, Any]:
    return {
        "embedded_tools": {
            "system_commands": {"enabled": True, "require_approval": command},
            "email": {"enabled": True, "require_approval": email},
        }
    }


# -- The check ---------------------------------------------------------------------------


class TestOff:
    def test_a_first_use_asks_when_the_setting_is_off(self, harness: Harness) -> None:
        accessor.configure(Source(False))
        harness.answer = "once"
        result = harness.use("web_search")
        assert harness.asked == ["web_search"]
        assert harness.ran == ["web_search"]
        assert not result.get("is_error")

    def test_the_default_is_off(self, harness: Harness) -> None:
        assert registry.spec(KEY).default is False
        accessor.configure(accessor.FileSettings(_Konfig({})))
        harness.use("web_search")
        assert harness.asked == ["web_search"]
        assert harness.ran == []


class _Konfig:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class TestOn:
    def test_a_first_use_runs_without_the_dialogue(self, harness: Harness) -> None:
        accessor.configure(Source(True))
        result = harness.use("web_search")
        assert harness.asked == []
        assert harness.ran == ["web_search"]
        assert "web_search ran" in result["content"] and not result.get("is_error")

    def test_an_mcp_tool_takes_the_same_check(self, harness: Harness) -> None:
        accessor.configure(Source(True))
        harness.use("live_server__echo")
        assert harness.asked == []
        assert harness.ran == ["live_server__echo"]

    def test_nothing_is_stored_so_switching_it_off_asks_again(self, harness: Harness) -> None:
        source = Source(True)
        accessor.configure(source)
        harness.use("web_search")
        assert harness.stored("web_search") is None
        source.on = False
        harness.use("web_search")
        assert harness.asked == ["web_search"]
        assert harness.ran == ["web_search"]

    def test_a_change_applies_on_the_next_call(self, harness: Harness) -> None:
        source = Source(False)
        accessor.configure(source)
        harness.use("web_fetch")
        assert harness.asked == ["web_fetch"] and harness.ran == []
        source.on = True
        harness.use("read_file")
        assert harness.asked == ["web_fetch"]
        assert harness.ran == ["read_file"]

    def test_an_unreadable_setting_asks(self, harness: Harness) -> None:
        accessor.configure(Broken())
        harness.use("web_search")
        assert harness.asked == ["web_search"]

    def test_no_settings_source_asks(self, harness: Harness) -> None:
        harness.use("web_search")
        assert harness.asked == ["web_search"]


class TestDeniesStay:
    def test_a_conversation_deny_stays(self, harness: Harness) -> None:
        harness.db.call(
            tool_permissions.set_tool_permission, harness.cid, "web_search", "denied", USER
        )
        accessor.configure(Source(True))
        result = harness.use("web_search")
        assert harness.ran == [] and harness.asked == []
        assert result["is_error"] and DENIED in result["content"]

    def test_a_global_deny_stays(self, harness: Harness) -> None:
        harness.db.call(tool_permissions.set_global_tool_permission, USER, "web_search", "denied")
        accessor.configure(Source(True))
        result = harness.use("web_search")
        assert harness.ran == [] and harness.asked == []
        assert result["is_error"] and DENIED in result["content"]

    def test_a_deny_from_the_dialogue_before_it_was_switched_on_stays(
        self, harness: Harness
    ) -> None:
        source = Source(False)
        accessor.configure(source)
        harness.use("web_search")  # the user answers "denied"
        source.on = True
        result = harness.use("web_search")
        assert harness.ran == [] and harness.asked == ["web_search"]
        assert result["is_error"]


class TestForcedPromptsStay:
    @pytest.mark.parametrize("name", ["run_command", "send_email"])
    def test_a_tool_needing_approval_still_asks(
        self, sync_db: Any, sqlite_settings: Any, name: str
    ) -> None:
        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        accessor.configure(Source(True))
        harness = Harness(sync_db, _approval())
        harness.use(name)
        assert harness.asked == [name]
        assert harness.ran == []

    @pytest.mark.parametrize("name", ["run_command", "send_email"])
    def test_it_still_asks_after_an_always_allow(
        self, sync_db: Any, sqlite_settings: Any, name: str
    ) -> None:
        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        accessor.configure(Source(True))
        harness = Harness(sync_db, _approval())
        harness.db.call(tool_permissions.set_tool_permission, harness.cid, name, "allowed", USER)
        harness.use(name)
        assert harness.asked == [name]

    @pytest.mark.parametrize(
        ("name", "config"),
        [("run_command", _approval(command=False)), ("send_email", _approval(email=False))],
    )
    def test_with_approval_off_it_is_auto_approved(
        self, sync_db: Any, sqlite_settings: Any, name: str, config: dict[str, Any]
    ) -> None:
        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        accessor.configure(Source(True))
        harness = Harness(sync_db, config)
        harness.use(name)
        assert harness.asked == []
        assert harness.ran == [name]

    @pytest.mark.parametrize("name", ["create_skill", "update_skill"])
    def test_skill_authoring_always_asks(self, harness: Harness, name: str) -> None:
        accessor.configure(Source(True))
        harness.use(name)
        assert harness.asked == [name]
        assert harness.ran == []

    @pytest.mark.parametrize("name", ["create_skill", "update_skill"])
    def test_skill_authoring_asks_after_a_global_allow(self, harness: Harness, name: str) -> None:
        harness.db.call(tool_permissions.set_global_tool_permission, USER, name, "allowed")
        accessor.configure(Source(True))
        harness.use(name)
        assert harness.asked == [name]


# -- The setting in each mode ------------------------------------------------------------


def _bridge(client: TestClient) -> Any:
    return client.app.state.sync_db  # type: ignore[attr-defined]


class TestPersonalSetting:
    def test_saving_the_toggle_applies_on_the_next_call(self, personal: TestClient) -> None:
        harness = Harness(_bridge(personal))
        harness.use("web_search")
        assert harness.asked == ["web_search"]

        assert personal.post("/settings/api/save", json={KEY: True}).status_code == 200
        harness.use("read_file")
        assert harness.asked == ["web_search"] and harness.ran == ["read_file"]

        assert personal.post("/settings/api/save", json={KEY: False}).status_code == 200
        harness.use("list_directory")
        assert harness.asked == ["web_search", "list_directory"]

    def test_the_page_says_what_the_toggle_does_and_what_still_asks(
        self, personal: TestClient
    ) -> None:
        html = personal.get("/settings").text
        assert KEY in html
        _help_is_complete(html)


def _help_is_complete(html: str) -> None:
    import html as html_lib

    text = html_lib.unescape(html)
    assert "first use of a tool in a conversation runs without asking" in text
    for still in ("denied", "run_command", "send_email", "require approval", "skill"):
        assert still in text, still


@pytest.mark.postgres
class TestServerSetting:
    def test_the_administrators_setting_applies_to_every_user_on_the_next_call(
        self, server: TestClient
    ) -> None:
        bridge = _bridge(server)
        assert mode.is_server()
        harness = Harness(bridge)
        other = Harness(bridge, user="user-other-example")
        harness.use("web_search")
        assert harness.asked == ["web_search"]

        assert server.post("/settings/api/instance", json={KEY: True}).status_code == 200
        harness.use("read_file")
        other.use("web_fetch")
        assert harness.ran == ["read_file"] and other.ran == ["web_fetch"]
        assert other.asked == []

        assert server.post("/settings/api/instance", json={KEY: False}).status_code == 200
        other.use("list_directory")
        assert other.asked == ["list_directory"]

    def test_someone_who_is_not_an_administrator_cannot_switch_it_on(
        self, server: TestClient
    ) -> None:
        access.set_access_check(Deny())
        assert server.post("/settings/api/instance", json={KEY: True}).status_code == 403
        assert accessor.get(KEY) is False
        harness = Harness(_bridge(server))
        harness.use("web_search")
        assert harness.asked == ["web_search"]

    def test_the_instance_page_says_what_the_toggle_does(self, server: TestClient) -> None:
        html = server.get("/admin/security").text
        assert KEY in html
        _help_is_complete(html)
