"""Tool categories follow the signed-in user's roles in server mode (A5).

``is_category_available`` stays the one decision point: in server mode a
category other than the local-only ones needs ``spark.tools.<category>`` in the
user's permissions, and no permissions at all (nobody signed in) refuses every
category. The permissions travel with the embedded tools configuration
(``_permissions``) to every route that offers or runs a tool. Personal mode is
unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from spark import mode
from spark.identity.permissions import ADMINISTRATOR, AUDITOR, POWER_USER, USER, role_permissions
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.tools import registry
from spark.tools.registry import ANY_ROLE, PERMISSIONS_KEY

CONFIG: dict[str, Any] = {"embedded_tools": {"web": {"enabled": True}}}
ONLY_MEMORY = frozenset({"spark.tools.memory"})


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(
        RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://db.example.com/x")
        )
    )
    yield
    mode.reset()


@pytest.fixture
def personal_mode(tmp_path: Path) -> Iterator[None]:
    mode.configure(
        RuntimeSettings(
            Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db")
        )
    )
    yield
    mode.reset()


@pytest.fixture
def web_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    import spark.tools.web as web

    def _reached(*args: object, **kwargs: object) -> str:
        raise AssertionError("a web tool ran without the user's permission")

    monkeypatch.setattr(web, "execute", _reached)


def _with(permissions: Any) -> dict[str, Any]:
    return {**CONFIG, PERMISSIONS_KEY: permissions}


# -- The decision point -------------------------------------------------------------------


class TestDecision:
    def test_permission_grants_the_category(self) -> None:
        assert registry.is_category_available(
            "web", mode=Mode.SERVER, permissions={"spark.tools.web"}
        )

    def test_missing_permission_refuses(self) -> None:
        assert not registry.is_category_available("web", mode=Mode.SERVER, permissions=ONLY_MEMORY)

    def test_no_permissions_refuses_every_category(self) -> None:
        for category in registry.TOOL_CATEGORIES:
            assert not registry.is_category_available(category, mode=Mode.SERVER), category
            assert not registry.is_category_available(
                category, mode=Mode.SERVER, permissions=None
            ), category

    def test_any_role_asks_only_the_mode(self) -> None:
        for category in registry.TOOL_CATEGORIES:
            expected = category not in registry.LOCAL_ONLY_CATEGORIES
            assert (
                registry.is_category_available(category, mode=Mode.SERVER, permissions=ANY_ROLE)
                is expected
            ), category

    @pytest.mark.parametrize("category", sorted(registry.LOCAL_ONLY_CATEGORIES))
    def test_local_only_refused_on_a_server_whatever_the_role(self, category: str) -> None:
        assert not registry.is_category_available(
            category, mode=Mode.SERVER, permissions=role_permissions(ADMINISTRATOR)
        )

    @pytest.mark.parametrize("permissions", [None, frozenset(), ONLY_MEMORY, ANY_ROLE])
    def test_personal_mode_ignores_permissions(self, permissions: Any) -> None:
        for category in registry.TOOL_CATEGORIES:
            assert registry.is_category_available(
                category, mode=Mode.PERSONAL, permissions=permissions
            )

    @pytest.mark.parametrize(
        ("role", "category", "allowed"),
        [
            (USER, "web", True),
            (USER, "documents", True),
            (USER, "agents", False),
            (POWER_USER, "agents", True),
            (AUDITOR, "web", False),
            (AUDITOR, "core", False),
            (ADMINISTRATOR, "skill_authoring", True),
        ],
    )
    def test_roles(self, role: str, category: str, allowed: bool) -> None:
        assert (
            registry.is_category_available(
                category, mode=Mode.SERVER, permissions=role_permissions(role)
            )
            is allowed
        )

    def test_tools_outside_the_categories_are_not_judged(self) -> None:
        assert registry.is_tool_available("some_mcp_tool", mode=Mode.SERVER, permissions=None)

    def test_role_refusal_message(self) -> None:
        reason = registry.unavailable_reason("web_search", mode=Mode.SERVER, permissions=None)
        assert reason == "Tool 'web_search' is not available: your role does not allow web tools."
        reason = registry.unavailable_reason(
            "create_skill", mode=Mode.SERVER, permissions=ONLY_MEMORY
        )
        assert reason == (
            "Tool 'create_skill' is not available: your role does not allow skill authoring tools."
        )

    def test_local_only_message_is_about_the_mode(self) -> None:
        reason = registry.unavailable_reason(
            "run_command", mode=Mode.SERVER, permissions=role_permissions(ADMINISTRATOR)
        )
        assert reason is not None and "not available in server mode" in reason

    def test_allowed_has_no_reason(self) -> None:
        assert (
            registry.unavailable_reason(
                "web_search", mode=Mode.SERVER, permissions={"spark.tools.web"}
            )
            is None
        )


class TestConfigPermissions:
    def test_reads_the_configuration_key(self) -> None:
        assert registry.tool_permissions(_with(ONLY_MEMORY)) == ONLY_MEMORY
        assert registry.tool_permissions(_with(ANY_ROLE)) is ANY_ROLE

    @pytest.mark.parametrize("value", [None, ["spark.tools.web"], "spark.tools.web", 1])
    def test_anything_else_is_no_permissions(self, value: Any) -> None:
        assert registry.tool_permissions(_with(value)) is None

    def test_absent_is_no_permissions(self) -> None:
        assert registry.tool_permissions(CONFIG) is None


# -- Offering ------------------------------------------------------------------------------


class TestOffered:
    def test_server_offers_what_the_role_allows(self, server_mode: None) -> None:
        names = {t["name"] for t in registry.get_builtin_tools(_with(ONLY_MEMORY))}
        assert names == set(registry.TOOL_CATEGORIES["memory"])

    def test_server_without_permissions_offers_no_builtin_tool(self, server_mode: None) -> None:
        assert registry.get_builtin_tools(CONFIG) == []

    def test_user_role(self, server_mode: None) -> None:
        names = {t["name"] for t in registry.get_builtin_tools(_with(role_permissions(USER)))}
        assert "web_search" in names
        assert "spawn_agent" not in names

    def test_personal_offers_everything(self, personal_mode: None) -> None:
        names = {t["name"] for t in registry.get_builtin_tools(CONFIG)}
        assert "web_search" in names

    def test_conversation_manager_offers_per_user(self, server_mode: None) -> None:
        from spark.core.conversation_manager import ConversationManager

        config = {"embedded_tools": {"web": {"enabled": True}, "agents": {"enabled": True}}}
        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=config
        )
        user = {t["name"] for t in mgr._get_all_tools(role_permissions(USER))}
        power = {t["name"] for t in mgr._get_all_tools(role_permissions(POWER_USER))}
        nobody = {t["name"] for t in mgr._get_all_tools(None)}
        assert "web_search" in user and "spawn_agent" not in user
        assert {"web_search", "spawn_agent"} <= power
        assert nobody == set()
        # The shared configuration never carries a user's permissions.
        assert PERMISSIONS_KEY not in mgr._embedded_tools_config


# -- Refusal at every route that runs a tool ------------------------------------------------


class TestRefused:
    def test_execute_builtin_tool(self, server_mode: None, web_never_runs: None) -> None:
        text, is_error = registry.execute_builtin_tool(
            "web_fetch", {"url": "https://example.com"}, _with(ONLY_MEMORY)
        )
        assert is_error is True
        assert "your role does not allow web tools" in text

    def test_execute_builtin_tool_without_permissions(
        self, server_mode: None, web_never_runs: None
    ) -> None:
        text, is_error = registry.execute_builtin_tool(
            "web_fetch", {"url": "https://example.com"}, CONFIG
        )
        assert is_error is True
        assert "your role does not allow web tools" in text

    def test_execute_builtin_tool_with_permission(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.tools.web as web

        monkeypatch.setattr(web, "execute", lambda *a, **k: "fetched")
        text, is_error = registry.execute_builtin_tool(
            "web_fetch", {"url": "https://example.com"}, _with(role_permissions(USER))
        )
        assert (text, is_error) == ("fetched", False)

    def test_conversation_manager(self, server_mode: None, web_never_runs: None) -> None:
        from spark.core.conversation_manager import ConversationManager

        mcp = MagicMock()
        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=CONFIG, mcp_manager=mcp
        )
        for permissions in (None, ONLY_MEMORY, role_permissions(AUDITOR)):
            text, is_error = mgr._call_tool(
                "web_fetch", {"url": "https://example.com"}, permissions=permissions
            )
            assert is_error is True
            assert "your role does not allow web tools" in text
        mcp.call_tool.assert_not_called()

    def test_conversation_manager_spawn_agent_needs_the_role(self, server_mode: None) -> None:
        from spark.core.conversation_manager import ConversationManager

        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=CONFIG
        )
        mgr._current_conversation_id = 1
        mgr._current_user_guid = "user-1"
        text, is_error = mgr._call_tool(
            "spawn_agent", {"task": "t", "agent_name": "a"}, permissions=role_permissions(USER)
        )
        assert is_error is True
        assert "your role does not allow agents tools" in text

    def test_conversation_manager_passes_permissions_to_the_tool(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.tools.web as web
        from spark.core.conversation_manager import ConversationManager

        monkeypatch.setattr(web, "execute", lambda *a, **k: "fetched")
        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=CONFIG
        )
        mgr._get_memory_index_for_tools = lambda: None  # type: ignore[method-assign]
        text, is_error = mgr._call_tool(
            "web_fetch", {"url": "https://example.com"}, permissions=role_permissions(USER)
        )
        assert (text, is_error) == ("fetched", False)

    def test_sub_agent_carries_its_users_permissions(
        self, server_mode: None, web_never_runs: None
    ) -> None:
        from spark.core.agent_executor import AgentExecutor

        mcp = MagicMock()
        agent = AgentExecutor(
            MagicMock(), MagicMock(), _with(ONLY_MEMORY), mcp_manager=mcp, mcp_loop=MagicMock()
        )
        text = agent._execute_tool("web_fetch", {"url": "https://example.com"})
        assert "your role does not allow web tools" in text
        mcp.call_tool.assert_not_called()

    def test_sub_agent_without_permissions(self, server_mode: None, web_never_runs: None) -> None:
        from spark.core.agent_executor import AgentExecutor

        agent = AgentExecutor(MagicMock(), MagicMock(), CONFIG)
        assert "your role does not allow web tools" in agent._execute_tool(
            "web_fetch", {"url": "https://example.com"}
        )

    def test_spawned_agent_gets_the_spawning_users_permissions(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.core import agent_executor
        from spark.core.conversation_manager import ConversationManager

        seen: dict[str, Any] = {}

        class FakeExecutor:
            def __init__(self, llm: Any, db: Any, config: dict[str, Any], **kwargs: Any) -> None:
                seen["config"] = config

            def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                return {"status": "completed", "result": "done"}

        monkeypatch.setattr(agent_executor, "AgentExecutor", FakeExecutor)
        mgr = ConversationManager(
            MagicMock(), MagicMock(), MagicMock(), embedded_tools_config=CONFIG
        )
        mgr._get_llm_service_for_model = lambda model_id: MagicMock()  # type: ignore[method-assign]
        power = role_permissions(POWER_USER)
        mgr._execute_agent_spawn(
            {"task": "t", "agent_name": "a", "model_id": "m-1"}, 1, "user-1", power
        )
        assert seen["config"][PERMISSIONS_KEY] == power
        assert PERMISSIONS_KEY not in mgr._embedded_tools_config

    def _action_executor(self, permissions: Any) -> Any:
        from spark.scheduler.executor import ActionExecutor

        ctx = MagicMock()
        ctx.settings.get = lambda key, default=None: (
            CONFIG["embedded_tools"] if key == "embedded_tools" else default
        )
        executor = ActionExecutor(
            ctx, "daemon-1", MagicMock(), permissions=permissions, user_guid="owner-guid"
        )
        executor._mcp_manager = MagicMock()
        executor._mcp_loop = MagicMock()
        return executor

    @pytest.mark.parametrize("permissions", [None, ONLY_MEMORY])
    def test_scheduled_action_follows_its_users_permissions(
        self, server_mode: None, web_never_runs: None, permissions: Any
    ) -> None:
        executor = self._action_executor(permissions)
        text = executor._execute_tool("web_fetch", {"url": "https://example.com"})
        assert "your role does not allow web tools" in text
        executor._mcp_manager.call_tool.assert_not_called()

    def test_scheduled_action_offers_what_its_user_may_use(self, server_mode: None) -> None:
        executor = self._action_executor(ONLY_MEMORY)
        executor._mcp_manager = None
        names = {t["name"] for t in executor._get_tools()}
        assert names == set(registry.TOOL_CATEGORIES["memory"])

    def test_scheduled_action_runs_with_the_permission(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.tools.web as web

        monkeypatch.setattr(web, "execute", lambda *a, **k: "fetched")
        executor = self._action_executor(role_permissions(USER))
        assert executor._execute_tool("web_fetch", {"url": "https://example.com"}) == "fetched"
