"""Where the web process takes its MCP servers from, and who else asks the manager.

Server mode builds the servers from the ``mcp.servers`` instance setting (and
the secrets store), never ``config.yaml``, and always starts the MCP loop.
Personal mode reads ``config.yaml`` as before. Sub-agents, debates and panels
pass their user's permissions, so on a server they see only granted servers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import spark.web.server as server
from spark import mode
from spark.instance_settings import registry
from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from spark.mcp_integration.runtime import MCPRuntime
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.tools.registry import PERMISSIONS_KEY
from tests.test_mcp_manager_scoping import FakeClient

STDIO = {"name": "local", "transport": "stdio", "command": "npx"}
HTTP = {"name": "search", "transport": "http", "url": "https://s.example.com/mcp", "roles": []}


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


@pytest.fixture
def config_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mcp": {"servers": [STDIO]}}), encoding="utf-8")
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    return path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[MCPRuntime]]:
    """Start runtimes without connecting anything; stop them afterwards."""
    runtimes: list[MCPRuntime] = []
    monkeypatch.setattr(MCPRuntime, "connect_all", lambda self, timeout=60.0: {})
    real_start = MCPRuntime.start.__func__  # type: ignore[attr-defined]

    def start(cls: type[MCPRuntime], manager: MCPManager) -> MCPRuntime:
        runtime = real_start(cls, manager)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(MCPRuntime, "start", classmethod(start))
    yield runtimes
    for runtime in runtimes:
        runtime.stop()


class Source:
    def get(self, key: str) -> Any:
        return [HTTP] if key == "mcp.servers" else registry.spec(key).default


def _app() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


class TestStartUp:
    def test_server_reads_the_instance_settings_not_config_yaml(
        self,
        server_mode: None,
        config_yaml: Path,
        started: list[MCPRuntime],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        app = _app()
        instance = SimpleNamespace(source=Source(), mcp_secret=lambda name: None)
        with caplog.at_level(logging.WARNING):
            runtime = server._start_mcp(app, SimpleNamespace(), instance)  # type: ignore[arg-type]
        assert runtime is not None
        assert set(runtime.manager.servers) == {"search"}
        assert app.state.mcp_manager is runtime.manager
        assert app.state._mcp_loop is runtime.loop
        assert "config.yaml" in caplog.text

    def test_server_starts_the_loop_with_no_servers(
        self, server_mode: None, started: list[MCPRuntime], tmp_path: Path, monkeypatch: Any
    ) -> None:
        import spark.core.application as app_mod

        monkeypatch.setattr(app_mod, "_get_config_path", lambda: tmp_path / "none.yaml")
        empty = SimpleNamespace(
            get=lambda key: [] if key == "mcp.servers" else registry.spec(key).default
        )
        runtime = server._start_mcp(
            _app(), SimpleNamespace(), SimpleNamespace(source=empty, mcp_secret=lambda n: None)  # type: ignore[arg-type]
        )
        assert runtime is not None and runtime.loop.is_running()

    def test_personal_mode_reads_config_yaml(
        self, config_yaml: Path, started: list[MCPRuntime]
    ) -> None:
        runtime = server._start_mcp(_app(), SimpleNamespace(), None)  # type: ignore[arg-type]
        assert runtime is not None
        assert set(runtime.manager.servers) == {"local"}

    def test_personal_mode_without_servers_starts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[MCPRuntime]
    ) -> None:
        import spark.core.application as app_mod

        monkeypatch.setattr(app_mod, "_get_config_path", lambda: tmp_path / "none.yaml")
        app = _app()
        assert server._start_mcp(app, SimpleNamespace(), None) is None  # type: ignore[arg-type]
        assert app.state.mcp_manager is None

    def test_from_config_on_a_server_ignores_config_yaml(
        self, server_mode: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            manager = MCPManager.from_config({"mcp": {"servers": [STDIO, HTTP]}})
        assert manager.servers == {}
        assert "config.yaml" in caplog.text

    def test_from_config_in_personal_mode_keeps_every_server(self) -> None:
        manager = MCPManager.from_config({"mcp": {"servers": [STDIO, HTTP]}})
        assert set(manager.servers) == {"local", "search"}


# -- Sub-agents, debates and panels ----------------------------------------------------


@pytest.fixture
def runtime() -> Iterator[MCPRuntime]:
    FakeClient.instances = []
    manager = MCPManager(client_factory=FakeClient)
    for name in ("search", "finance"):
        manager.add_server(
            MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com/mcp")
        )
    rt = MCPRuntime.start(manager)
    rt.run(manager.connect_all())
    rt.run(manager.list_all_tools())
    yield rt
    rt.stop()


def _config(*granted: str) -> dict[str, Any]:
    return {"embedded_tools": {}, PERMISSIONS_KEY: frozenset(granted)}


class TestCallers:
    def test_a_sub_agent_sees_and_calls_only_granted_servers(
        self, server_mode: None, runtime: MCPRuntime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.core.agent_executor import AgentExecutor

        events: list[Any] = []
        from spark.identity import audit

        monkeypatch.setattr(
            audit, "_current", SimpleNamespace(submit=lambda *a, **k: events.append((a, k)))
        )
        agent = AgentExecutor(
            None,
            None,
            _config("spark.mcp.search"),
            mcp_manager=runtime.manager,
            mcp_loop=runtime.loop,
            user_guid="u-1",
            conversation_id=9,
        )
        names = {t["name"] for t in agent._get_tools()}
        assert "search_lookup" in names and "finance_lookup" not in names
        assert agent._execute_tool("search_lookup", {}) == "ok"
        assert "not available" in agent._execute_tool("finance_lookup", {})
        assert [k["user_guid"] for _, k in events] == ["u-1", "u-1"]
        assert [k["details"]["conversation_id"] for _, k in events] == [9, 9]

    @pytest.mark.parametrize(
        "module", ["spark.core.debate.orchestrator", "spark.core.panel.orchestrator"]
    )
    def test_orchestrators_see_only_granted_tool_names(
        self, server_mode: None, runtime: MCPRuntime, module: str
    ) -> None:
        import importlib

        mod = importlib.import_module(module)
        cls = next(
            getattr(mod, n)
            for n in dir(mod)
            if n.endswith("Orchestrator") and isinstance(getattr(mod, n), type)
        )
        orchestrator = cls.__new__(cls)
        orchestrator._config = _config("spark.mcp.finance")
        orchestrator._mcp_manager = runtime.manager
        names = orchestrator._available_tool_names()
        assert "finance_lookup" in names and "search_lookup" not in names

    def test_runtime_runs_on_its_own_loop(self, runtime: MCPRuntime) -> None:
        async def which() -> asyncio.AbstractEventLoop:
            return asyncio.get_running_loop()

        assert runtime.run(which()) is runtime.loop
