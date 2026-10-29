"""Tool availability by mode: local-only tools do not exist in server mode.

In server mode the system command, filesystem and email tools are not
registered and are refused by name from every route that can request a tool
(a model turn, a sub-agent, a scheduled action, the settings API). Personal
mode keeps them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from spark import mode
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.tools import registry
from tests.web_helpers import sign_in

LOCAL_TOOLS = (
    "run_command",
    "read_file",
    "write_file",
    "list_directory",
    "search_files",
    "get_file_info",
    "find_in_file",
    "get_directory_tree",
    "send_email",
    "draft_email",
)

# Every local-only tool enabled and configured, as a personal-mode user might have it.
FULL_CONFIG: dict[str, Any] = {
    "embedded_tools": {
        "filesystem": {"enabled": True, "allowed_paths": ["/srv/data"], "mode": "read_write"},
        "system_commands": {"enabled": True, "require_approval": False},
        "email": {"enabled": True, "host": "smtp.example.com", "sender": "spark@example.com"},
        "web": {"enabled": True},
    }
}


def _server() -> RuntimeSettings:
    return RuntimeSettings(
        Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://db.example.com/spark")
    )


def _personal(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "spark.db")
    )


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(_server())
    yield
    mode.reset()


@pytest.fixture
def personal_mode(tmp_path: Path) -> Iterator[None]:
    mode.configure(_personal(tmp_path))
    yield
    mode.reset()


@pytest.fixture
def tools_never_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a local-only tool implementation is reached."""

    def _reached(*args: object, **kwargs: object) -> str:
        raise AssertionError("a local-only tool ran in server mode")

    import spark.tools.email_tool as email_tool
    import spark.tools.filesystem as filesystem
    import spark.tools.system_command as system_command

    monkeypatch.setattr(system_command, "execute", _reached)
    monkeypatch.setattr(filesystem, "execute", _reached)
    monkeypatch.setattr(email_tool, "execute", _reached)


# -- The availability function ---------------------------------------------------------


class TestCategories:
    def test_every_local_tool_has_a_local_category(self) -> None:
        for name in LOCAL_TOOLS:
            assert registry.tool_category(name) in registry.LOCAL_ONLY_CATEGORIES, name

    def test_local_only_categories(self) -> None:
        assert registry.LOCAL_ONLY_CATEGORIES == frozenset(
            {"filesystem", "system_commands", "email"}
        )

    def test_every_builtin_tool_has_a_category(self, personal_mode: None) -> None:
        for tool in registry.get_builtin_tools(FULL_CONFIG):
            assert registry.tool_category(tool["name"]) is not None, tool["name"]

    def test_permission_names(self) -> None:
        assert registry.permission_name("system_commands") == "spark.tools.system_commands"
        assert registry.permission_name("web") == "spark.tools.web"

    def test_unknown_tool_has_no_category(self) -> None:
        assert registry.tool_category("some_mcp_tool") is None


class TestAvailability:
    @pytest.mark.parametrize("category", ["filesystem", "system_commands", "email"])
    def test_local_categories_unavailable_in_server_mode(self, category: str) -> None:
        assert registry.is_category_available(category, mode=Mode.SERVER) is False

    @pytest.mark.parametrize("category", ["filesystem", "system_commands", "email"])
    def test_local_categories_available_in_personal_mode(self, category: str) -> None:
        assert registry.is_category_available(category, mode=Mode.PERSONAL) is True

    @pytest.mark.parametrize("category", ["web", "documents", "archives", "memory", "core"])
    def test_other_categories_available_in_server_mode(self, category: str) -> None:
        assert registry.is_category_available(category, mode=Mode.SERVER) is True

    def test_reads_the_configured_mode(self, server_mode: None) -> None:
        assert registry.is_tool_available("run_command") is False
        assert registry.is_tool_available("web_search") is True

    def test_unconfigured_mode_is_personal(self) -> None:
        mode.reset()
        assert registry.is_tool_available("run_command") is True

    def test_unknown_tools_are_not_refused_by_mode(self, server_mode: None) -> None:
        assert registry.unavailable_reason("some_mcp_tool") is None

    def test_refusal_message(self, server_mode: None) -> None:
        reason = registry.unavailable_reason("run_command")
        assert reason == (
            "Tool 'run_command' is not available: system command tools are not "
            "available in server mode."
        )


# -- Registration ----------------------------------------------------------------------


class TestRegistration:
    def test_server_mode_registers_no_local_tools(self, server_mode: None) -> None:
        names = {t["name"] for t in registry.get_builtin_tools(FULL_CONFIG)}
        assert names.isdisjoint(LOCAL_TOOLS)
        assert "web_search" in names

    def test_personal_mode_registers_local_tools(self, personal_mode: None) -> None:
        names = {t["name"] for t in registry.get_builtin_tools(FULL_CONFIG)}
        assert set(LOCAL_TOOLS) <= names


# -- Refusal by name -------------------------------------------------------------------


class TestRefusedByName:
    @pytest.mark.parametrize("tool_name", LOCAL_TOOLS)
    def test_registry_refuses(
        self, server_mode: None, tools_never_run: None, tool_name: str
    ) -> None:
        text, is_error = registry.execute_builtin_tool(
            tool_name, {"command": "hostname"}, FULL_CONFIG
        )
        assert is_error is True
        assert "not available in server mode" in text

    @pytest.mark.parametrize("tool_name", ["run_command", "read_file", "send_email"])
    def test_conversation_manager_refuses(
        self, server_mode: None, tools_never_run: None, tool_name: str
    ) -> None:
        from spark.core.conversation_manager import ConversationManager

        mcp = MagicMock()
        mgr = ConversationManager(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            embedded_tools_config=FULL_CONFIG,
            mcp_manager=mcp,
        )
        text, is_error = mgr._call_tool(tool_name, {"command": "hostname"})
        assert is_error is True
        assert "not available in server mode" in text
        mcp.call_tool.assert_not_called()

    @pytest.mark.parametrize("tool_name", ["run_command", "read_file", "send_email"])
    def test_sub_agent_refuses(
        self, server_mode: None, tools_never_run: None, tool_name: str
    ) -> None:
        from spark.core.agent_executor import AgentExecutor

        mcp = MagicMock()
        agent = AgentExecutor(
            MagicMock(), MagicMock(), FULL_CONFIG, mcp_manager=mcp, mcp_loop=MagicMock()
        )
        text = agent._execute_tool(tool_name, {"command": "hostname"})
        assert "not available in server mode" in text
        mcp.call_tool.assert_not_called()

    @pytest.mark.parametrize("tool_name", ["run_command", "read_file", "send_email"])
    def test_scheduled_action_refuses(
        self, server_mode: None, tools_never_run: None, tool_name: str
    ) -> None:
        from spark.scheduler.executor import ActionExecutor

        executor = ActionExecutor.__new__(ActionExecutor)
        executor._ctx = MagicMock()
        executor._ctx.settings.get = lambda key, default=None: (
            FULL_CONFIG["embedded_tools"] if key == "embedded_tools" else default
        )
        executor._db = MagicMock()
        executor._user_guid = "user-1"
        executor._mcp_manager = MagicMock()
        executor._mcp_loop = MagicMock()
        text = executor._execute_tool(tool_name, {"command": "hostname"})
        assert "not available in server mode" in text
        executor._mcp_manager.call_tool.assert_not_called()

    def test_personal_mode_still_dispatches(
        self, personal_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.tools.system_command as system_command

        monkeypatch.setattr(system_command, "execute", lambda *a, **k: "ran")
        text, is_error = registry.execute_builtin_tool(
            "run_command", {"command": "hostname"}, FULL_CONFIG
        )
        assert (text, is_error) == ("ran", False)


class TestSystemPrompt:
    def test_server_mode_does_not_advertise_local_tools(self, server_mode: None) -> None:
        from spark.core.conversation_manager import ConversationManager

        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=FULL_CONFIG
        )
        context = mgr._build_conversation_tool_context()
        assert "System commands" not in context
        assert "Filesystem access" not in context
        assert "Email" not in context

    def test_personal_mode_advertises_local_tools(self, personal_mode: None) -> None:
        from spark.core.conversation_manager import ConversationManager

        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=FULL_CONFIG
        )
        context = mgr._build_conversation_tool_context()
        assert "System commands" in context
        assert "Filesystem access" in context
        assert "Email" in context


# -- Settings API and page -------------------------------------------------------------


def _settings_get(values: dict[str, Any]) -> Any:
    base: dict[str, Any] = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
        "interface.ssl.enabled": False,
        "embedded_tools.system_commands.enabled": True,
        "embedded_tools.email.enabled": True,
        "embedded_tools.email.host": "smtp.example.com",
    }
    base.update(values)

    def get(key: str, default: object = None, *, cast: type | None = None) -> object:
        val = base.get(key, default)
        if cast is not None and val is not None:
            val = cast(val)
        return val

    return get


@pytest.fixture
def web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, str], Path]]:
    from spark.web.server import create_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text("embedded_tools: {}\n", encoding="utf-8")
    monkeypatch.setattr("spark.core.application._get_config_path", lambda: config_path)
    ctx = MagicMock()
    ctx.settings.get = _settings_get({})
    app = create_app(ctx, first_run=False)
    client = TestClient(app)
    cookies = sign_in(client)
    yield client, cookies, config_path


LOCAL_SETTING_KEYS = [
    "embedded_tools.system_commands.enabled",
    "embedded_tools.system_commands.blocked_commands",
    "embedded_tools.filesystem.allowed_paths",
    "embedded_tools.filesystem.enabled",
    "embedded_tools.email.host",
    "embedded_tools.email.enabled",
]


class TestSettingsApi:
    @pytest.mark.parametrize("key", LOCAL_SETTING_KEYS)
    def test_server_mode_refuses_local_tool_settings(
        self, server_mode: None, web: tuple[TestClient, dict[str, str], Path], key: str
    ) -> None:
        client, cookies, config_path = web
        before = config_path.read_bytes()
        resp = client.post("/settings/api/save", json={key: "true"}, cookies=cookies)
        assert resp.status_code == 403
        assert "server mode" in resp.json()["detail"]
        assert config_path.read_bytes() == before

    @pytest.mark.parametrize(
        "payload",
        [
            {"embedded_tools": {"system_commands": {"enabled": True}}},
            {"embedded_tools.email": {"enabled": True, "host": "smtp.example.com"}},
        ],
    )
    def test_server_mode_refuses_nested_local_tool_settings(
        self,
        server_mode: None,
        web: tuple[TestClient, dict[str, str], Path],
        payload: dict[str, Any],
    ) -> None:
        client, cookies, config_path = web
        before = config_path.read_bytes()
        resp = client.post("/settings/api/save", json=payload, cookies=cookies)
        assert resp.status_code == 403
        assert config_path.read_bytes() == before

    def test_server_mode_sends_other_settings_to_the_instance_endpoint(
        self, server_mode: None, web: tuple[TestClient, dict[str, str], Path]
    ) -> None:
        """A server keeps its settings in the database (instance settings), not config.yaml."""
        client, cookies, config_path = web
        before = config_path.read_bytes()
        resp = client.post(
            "/settings/api/save", json={"embedded_tools.web.enabled": True}, cookies=cookies
        )
        assert resp.status_code == 409
        assert "/settings/api/instance" in resp.json()["detail"]
        assert config_path.read_bytes() == before

    def test_personal_mode_saves_local_tool_settings(
        self, personal_mode: None, web: tuple[TestClient, dict[str, str], Path]
    ) -> None:
        client, cookies, config_path = web
        resp = client.post(
            "/settings/api/save",
            json={"embedded_tools.system_commands.enabled": True},
            cookies=cookies,
        )
        assert resp.status_code == 200
        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["embedded_tools"]["system_commands"]["enabled"] is True

    def test_server_mode_refuses_email_test(
        self, server_mode: None, web: tuple[TestClient, dict[str, str], Path]
    ) -> None:
        client, cookies, _ = web
        resp = client.post("/settings/api/test-email", json={}, cookies=cookies)
        assert resp.status_code == 403

    def test_server_mode_refuses_directory_browsing(
        self, server_mode: None, web: tuple[TestClient, dict[str, str], Path]
    ) -> None:
        client, cookies, _ = web
        resp = client.get("/settings/api/browse-directory", cookies=cookies)
        assert resp.status_code == 403


class TestSettingsPage:
    def test_server_mode_hides_local_tool_sections(self, server_mode: None) -> None:
        from spark.web.endpoints.settings import _build_tool_categories

        ids = {c["id"] for c in _build_tool_categories(MagicMock(get=_settings_get({})))}
        assert ids.isdisjoint({"filesystem", "system_commands", "email"})
        assert "web" in ids

    def test_personal_mode_shows_local_tool_sections(self, personal_mode: None) -> None:
        from spark.web.endpoints.settings import _build_tool_categories

        ids = {c["id"] for c in _build_tool_categories(MagicMock(get=_settings_get({})))}
        assert {"filesystem", "system_commands", "email"} <= ids
