"""Server mode: the MCP servers follow ``mcp.servers`` live, on every replica (D37, A12).

The server list is the administrator's ``mcp.servers`` instance setting with
credentials from the secrets store, never ``config.yaml``. Every reload or
change of the settings (this replica's own, or the listener's after another
replica's) reconnects the servers whose entry or credentials changed, through
the :class:`~spark.instance_settings.consumers.SettingsFollower`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.instance_settings import consumers, registry
from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from spark.mcp_integration.runtime import MCPRuntime
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.mcp_stub_server import StubServer, serving


class Source:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(values or {})
        self.observers: list[Any] = []

    def get(self, key: str) -> Any:
        return self.values.get(key, registry.spec(key).default)

    def on_change(self, observer: Any) -> None:
        self.observers.append(observer)

    def change(self, key: str, value: Any) -> None:
        self.values[key] = value
        for observer in self.observers:
            observer()


class Secrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def resolve(self, key: str) -> str:
        return ""


class Target:
    def __init__(self) -> None:
        self.applied: list[list[MCPServerConfig]] = []
        self.lock = threading.Lock()

    def apply(self, configs: list[MCPServerConfig]) -> set[str]:
        with self.lock:
            self.applied.append(list(configs))
        return {c.name for c in configs}


class Llm:
    def replace_providers(self, providers: Any) -> None: ...

    def set_offered(self, offered: Any) -> None: ...


@pytest.fixture(autouse=True)
def _server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


def _entry(name: str = "search", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "transport": "http",
        "url": f"https://{name}.example.com/mcp",
        "roles": ["user"],
        **extra,
    }


def _follow(source: Source, store: dict[str, str], target: Target) -> consumers.SettingsFollower:
    return consumers.follow(
        source,
        Secrets(),
        llm_manager=Llm(),
        conversation_manager=None,
        build_providers=lambda view: [],
        mcp=target,
        mcp_secrets=store.get,
    )


class TestServerConfigs:
    def test_configs_come_from_the_setting_and_the_store(self) -> None:
        source = Source({"mcp.servers": [_entry(auth_type="bearer")]})
        configs = consumers.mcp_server_configs(source, {"mcp_server_search_auth_token": "t"}.get)
        assert [(c.name, c.transport, c.auth_token) for c in configs] == [("search", "http", "t")]

    def test_never_reads_config_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import yaml

        monkeypatch.setattr(yaml, "safe_load", lambda *a, **k: pytest.fail("read config.yaml"))
        assert consumers.mcp_server_configs(Source(), {}.get) == []


class TestFollower:
    def test_a_change_to_the_servers_reconnects(self) -> None:
        source, target = Source({"mcp.servers": [_entry()]}), Target()
        follower = _follow(source, {}, target)
        assert follower.wait_idle()
        target.applied.clear()
        source.change("mcp.servers", [_entry(), _entry("kb")])
        assert follower.wait_idle()
        assert [[c.name for c in configs] for configs in target.applied] == [["search", "kb"]]

    def test_an_unrelated_change_does_not_reconnect(self) -> None:
        source, target = Source({"mcp.servers": [_entry()]}), Target()
        follower = _follow(source, {}, target)
        assert follower.wait_idle()
        target.applied.clear()
        source.change("conversation.max_tool_iterations", 9)
        assert follower.wait_idle()
        assert target.applied == []

    def test_a_credential_change_reconnects(self) -> None:
        store: dict[str, str] = {}
        source, target = Source({"mcp.servers": [_entry(auth_type="bearer")]}), Target()
        follower = _follow(source, store, target)
        assert follower.wait_idle()
        target.applied.clear()
        store["mcp_server_search_auth_token"] = "new"
        follower.changed()  # the instance page calls this after writing a secret
        assert follower.wait_idle()
        assert [c.auth_token for c in target.applied[-1]] == ["new"]

    def test_without_an_mcp_target_nothing_is_reconnected(self) -> None:
        source = Source({"mcp.servers": [_entry()]})
        follower = consumers.follow(
            source,
            Secrets(),
            llm_manager=Llm(),
            conversation_manager=None,
            build_providers=lambda view: [],
        )
        source.change("mcp.servers", [])
        assert follower.wait_idle()


class TestRuntime:
    """The runtime keeps the sessions on one loop thread; changes are applied there."""

    def test_apply_connects_changes_and_drops_servers(self) -> None:
        with serving() as stub:
            runtime = MCPRuntime.start(MCPManager())
            try:
                assert runtime.apply(
                    [MCPServerConfig(name="search", transport="http", url=stub.http_url)]
                ) == {"search"}
                assert runtime.manager.servers["search"].connected
                changed = runtime.apply(
                    [
                        MCPServerConfig(name="search", transport="http", url=stub.http_url),
                        MCPServerConfig(name="kb", transport="sse", url=stub.sse_url),
                    ]
                )
                assert changed == {"kb"}
                tools = runtime.run(runtime.manager.list_all_tools(permissions={"spark.mcp.kb"}))
                assert {t["server"] for t in tools} == {"kb"}
                assert runtime.apply([]) == {"search", "kb"}
                assert runtime.manager.servers == {}
            finally:
                runtime.stop()
            assert not runtime.loop.is_running()

    def test_stop_disconnects_from_another_thread(self, stub_server: StubServer) -> None:
        manager = MCPManager()
        manager.add_server(MCPServerConfig(name="s", transport="sse", url=stub_server.sse_url))
        runtime = MCPRuntime.start(manager)
        runtime.connect_all()
        client = manager.servers["s"]
        assert client.connected
        runtime.stop()
        deadline = time.monotonic() + 5
        while client.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not client.connected


@pytest.fixture
def stub_server() -> Iterator[StubServer]:
    with serving() as stub:
        yield stub
