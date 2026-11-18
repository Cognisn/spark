"""SPARK's MCP servers on conduit's MCP client (M2), end to end against local stub servers.

Every session lives on the process's conduit loop (the connection runtime's), with
no MCP loop thread of its own; synchronous callers use conduit's facade. stdio is
for personal mode only. Credentials reach conduit as ``secret://`` references,
resolved through SPARK's resolver. A result is framed as untrusted content once,
by SPARK (A16). A server unreachable at start-up leaves the others working, and a
change to ``mcp.servers`` or a credential reconnects live (D37). MCP servers here
are the stubs in ``tests/mcp_stub_server.py`` and ``tests/mcp_stdio_stub.py``, on
127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from conduit import FacadeError, LiteralSecretResolver, LoopThread

import spark.web.server as server
from spark import mode
from spark.instance_settings import consumers, registry
from spark.mcp_integration.client import MCPClient, StdioRefused, conduit_name
from spark.mcp_integration.manager import MCPManager, MCPServerConfig
from spark.mcp_integration.runtime import MCPRuntime
from spark.mcp_integration.servers import ServerEntryError, validate_entry
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.tools.registry import ANY_ROLE, is_mcp_server_available
from tests.mcp_stub_server import HOST, StubServer, serving

STDIO_STUB = str(Path(__file__).with_name("mcp_stdio_stub.py"))
TOKEN = "example-token"  # noqa: S105 - synthetic


@pytest.fixture(scope="module")
def stub() -> Iterator[StubServer]:
    with serving() as server_:
        yield server_


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


@pytest.fixture
def conduit_loop() -> Iterator[LoopThread]:
    loop = LoopThread(name="test-conduit")
    yield loop
    loop.close()


def _dead_port() -> int:
    """A loopback port nothing listens on (next to the slot's stub port when set)."""
    configured = os.environ.get("SPARK_TEST_MCP_PORT")
    if configured:
        return int(configured) + 1
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def _http(stub: StubServer, name: str = "search", **extra: Any) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="http", url=stub.http_url, timeout=15, **extra)


async def _running() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


# -- The conduit loop ------------------------------------------------------------------


class TestConduitLoop:
    def test_start_up_binds_the_manager_to_the_connection_runtimes_loop(
        self, server_mode: None, conduit_loop: LoopThread, tmp_path: Path, monkeypatch: Any
    ) -> None:
        import spark.core.application as app_mod

        monkeypatch.setattr(app_mod, "_get_config_path", lambda: tmp_path / "none.yaml")
        app = SimpleNamespace(state=SimpleNamespace(connections=SimpleNamespace(loop=conduit_loop)))
        source = SimpleNamespace(
            get=lambda key: [] if key == "mcp.servers" else registry.spec(key).default
        )
        instance = SimpleNamespace(source=source, mcp_secret=lambda name: None)
        runtime = server._start_mcp(app, SimpleNamespace(), instance)  # type: ignore[arg-type]
        assert runtime is not None
        try:
            assert runtime.conduit_loop is conduit_loop
            assert runtime.loop is conduit_loop.run(_running())
            assert app.state._mcp_loop is runtime.loop
            assert runtime.manager.loop is runtime.loop
            assert not [t for t in threading.enumerate() if t.name == "spark-mcp-loop"]
        finally:
            runtime.stop()
        # The connection runtime owns the loop: stopping MCP leaves it running.
        assert conduit_loop.run(_running()) is runtime.loop

    async def test_a_call_from_another_loop_runs_on_the_conduit_loop(self) -> None:
        seen: list[asyncio.AbstractEventLoop] = []

        class Recording:
            def __init__(self, config: MCPServerConfig) -> None:
                self.config, self.name, self.connected = config, config.name, False

            async def connect(self) -> bool:
                seen.append(asyncio.get_running_loop())
                self.connected = True
                return True

            async def disconnect(self) -> None:
                seen.append(asyncio.get_running_loop())
                self.connected = False

            async def list_tools(self) -> list[dict[str, Any]]:
                seen.append(asyncio.get_running_loop())
                return []

            async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                raise AssertionError("not called")

        manager = MCPManager(client_factory=Recording)
        manager.add_server(MCPServerConfig(name="a", transport="http", url="https://a.example.com"))
        runtime = MCPRuntime.start(manager)
        try:
            await manager.connect_all()
            await manager.list_all_tools()
            await manager.disconnect_all()
        finally:
            runtime.stop()
        assert seen and all(loop is runtime.loop for loop in seen)
        assert asyncio.get_running_loop() is not runtime.loop

    def test_sync_callers_use_the_facade_which_refuses_the_loop_itself(self) -> None:
        runtime = MCPRuntime.start(MCPManager())
        try:

            async def nested() -> None:
                runtime.run(runtime.manager.disconnect_all())

            with pytest.raises(FacadeError):
                runtime.run(nested())
        finally:
            runtime.stop()


# -- Transports and credentials ----------------------------------------------------------


class TestCredentials:
    async def test_a_secret_reference_resolves_through_sparks_resolver(
        self, stub: StubServer
    ) -> None:
        stub.headers.clear()
        secrets = LiteralSecretResolver({"example_mcp_token": TOKEN})
        client = MCPClient(
            _http(stub, auth_type="bearer", auth_token="secret://example_mcp_token"),
            secrets=secrets,
        )
        assert await client.connect()
        await client.disconnect()
        assert {"authorization": f"Bearer {TOKEN}"} in stub.headers

    async def test_conduit_holds_only_references(self, stub: StubServer) -> None:
        settings, _ = MCPClient(_http(stub, auth_type="bearer", auth_token=TOKEN)).settings()
        assert TOKEN not in settings.model_dump_json()
        assert str(settings.auth.token).startswith("secret://")  # type: ignore[union-attr]

    async def test_an_unresolvable_reference_does_not_connect(self, stub: StubServer) -> None:
        client = MCPClient(_http(stub, auth_type="bearer", auth_token="secret://missing"))
        assert await client.connect() is False

    @pytest.mark.parametrize("transport", ["http", "sse"])
    async def test_api_key_and_basic_credentials_are_sent(
        self, stub: StubServer, transport: str
    ) -> None:
        url = stub.http_url if transport == "http" else stub.sse_url
        stub.headers.clear()
        api_key = MCPClient(
            MCPServerConfig(
                name="k",
                transport=transport,
                url=url,
                auth_type="api_key",
                auth_token=TOKEN,
                auth_header_name="X-Key",
                timeout=15,
            )
        )
        assert await api_key.connect()
        await api_key.disconnect()
        basic = MCPClient(
            MCPServerConfig(
                name="b",
                transport=transport,
                url=url,
                auth_type="basic",
                basic_username="ada",
                basic_password="pw",  # noqa: S106 - synthetic
                timeout=15,
            )
        )
        assert await basic.connect()
        await basic.disconnect()
        assert {"x-key": TOKEN} in stub.headers
        assert {"authorization": "Basic YWRhOnB3"} in stub.headers

    async def test_plain_http_beyond_this_machine_is_refused(self) -> None:
        client = MCPClient(
            MCPServerConfig(name="far", transport="http", url="http://192.0.2.10/mcp", timeout=3)
        )
        assert await client.connect() is False

    def test_a_server_entry_with_plain_http_beyond_this_machine_is_refused(self) -> None:
        entry = {"name": "far", "transport": "http", "url": "http://192.0.2.10/mcp"}
        with pytest.raises(ServerEntryError, match="loopback"):
            validate_entry(entry, server=True)
        assert validate_entry({**entry, "url": "https://far.example.com/mcp"}, server=True)

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("search", "search"), ("my-server", "my_server"), ("9lives", "mcp_9lives"), ("--", "mcp")],
    )
    def test_conduit_names(self, name: str, expected: str) -> None:
        assert conduit_name(name) == expected

    def test_conduit_names_fit_conduits_limit(self) -> None:
        assert len(conduit_name("a" * 63)) == 32


# -- stdio: personal mode only -----------------------------------------------------------


def _stdio(**extra: Any) -> MCPServerConfig:
    return MCPServerConfig(
        name="local",
        transport="stdio",
        command=sys.executable,
        args=[STDIO_STUB],
        timeout=30,
        **extra,
    )


class TestStdio:
    async def test_personal_mode_runs_a_stdio_server_with_its_environment(self) -> None:
        client = MCPClient(_stdio(env={"EXAMPLE_API_KEY": TOKEN, "PLAIN": "value"}))
        assert await client.connect()
        try:
            names = {t["name"] for t in await client.list_tools()}
            secret = await client.call_tool("env", {"name": "EXAMPLE_API_KEY"})
            plain = await client.call_tool("env", {"name": "PLAIN"})
        finally:
            await client.disconnect()
        assert names == {"echo", "env"}
        assert secret["content"][0]["text"] == TOKEN
        assert plain["content"][0]["text"] == "value"

    async def test_server_mode_refuses_stdio_in_the_client(self, server_mode: None) -> None:
        client = MCPClient(_stdio())
        with pytest.raises(StdioRefused):
            client.settings()
        assert await client.connect() is False
        assert not client.connected


# -- Single framing (A16) ----------------------------------------------------------------


class TestFraming:
    async def test_the_client_returns_the_result_unframed(self, stub: StubServer) -> None:
        client = MCPClient(_http(stub))
        assert await client.connect()
        try:
            result = await client.call_tool("echo", {"text": "hello"})
        finally:
            await client.disconnect()
        assert result == {"content": [{"type": "text", "text": "echo: hello"}], "isError": False}

    def test_a_result_reaches_the_model_framed_once_by_spark(
        self, stub: StubServer, sync_db: Any
    ) -> None:
        from spark.core.conversation_manager import ConversationManager
        from spark.database import conversations

        manager = MCPManager()
        manager.add_server(_http(stub))
        runtime = MCPRuntime.start(manager)
        try:
            runtime.connect_all()
            cm = ConversationManager(
                sync_db, MagicMock(), mcp_manager=manager, mcp_loop=runtime.loop
            )
            cid = sync_db.call(conversations.create_conversation, "Tools", "m", "user-ada")
            result = cm._execute_single_tool(
                {"id": "t1", "name": "echo", "input": {"text": "hello"}}, cid, "user-ada"
            )
        finally:
            runtime.stop()
        content = str(result["content"])
        assert "echo: hello" in content
        assert content.count("<untrusted-content ") == 1
        assert "<<UNTRUSTED" not in content


# -- Start-up and live reconnect ---------------------------------------------------------


class TestStartUpAndReconnect:
    def test_a_server_unreachable_at_start_up_leaves_the_others_working(
        self,
        stub: StubServer,
        conduit_loop: LoopThread,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import spark.core.application as app_mod

        path = tmp_path / "config.yaml"
        down = f"http://{HOST}:{_dead_port()}/mcp"
        servers = [
            {"name": "up", "transport": "http", "url": stub.http_url, "timeout": 15},
            {"name": "down", "transport": "http", "url": down, "timeout": 5},
        ]
        path.write_text(yaml.safe_dump({"mcp": {"servers": servers}}), encoding="utf-8")
        monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
        app = SimpleNamespace(state=SimpleNamespace(connections=SimpleNamespace(loop=conduit_loop)))
        runtime = server._start_mcp(app, SimpleNamespace(), None)  # type: ignore[arg-type]
        assert runtime is not None
        try:
            assert runtime.manager.servers["up"].connected
            assert not runtime.manager.servers["down"].connected
            assert {t["server"] for t in runtime.manager.cached_tools()} == {"up"}
            result = runtime.run(runtime.manager.call_tool("echo", {"text": "x"}))
            assert result["content"][0]["text"] == "echo: x"
            assert "not reachable at start-up: down" in caplog.text
        finally:
            runtime.stop()

    def test_a_settings_change_reconnects_live(self, stub: StubServer, server_mode: None) -> None:
        store = {"mcp_server_search_auth_token": "first-token"}
        entry = {
            "name": "search",
            "transport": "http",
            "url": stub.http_url,
            "auth_type": "bearer",
            "roles": ["user"],
        }
        source = _Source({"mcp.servers": [entry]})
        runtime = MCPRuntime.start(MCPManager())
        try:
            runtime.apply(consumers.mcp_server_configs(source, store.get))
            follower = consumers.follow(
                source,
                SimpleNamespace(resolve=lambda key: ""),
                connections=None,
                conversation_manager=None,
                mcp=runtime,
                mcp_secrets=store.get,
            )
            first = runtime.manager.servers["search"]
            assert first.connected

            # A credential changes: the server reconnects with it.
            stub.headers.clear()
            store["mcp_server_search_auth_token"] = "second-token"
            follower.changed()
            assert follower.wait_idle()
            second = runtime.manager.servers["search"]
            assert second is not first and second.connected and not first.connected
            assert {"authorization": "Bearer second-token"} in stub.headers

            # A server is added, then the first removed.
            kb = {**entry, "name": "kb", "transport": "sse", "url": stub.sse_url}
            source.change("mcp.servers", [entry, kb])
            assert follower.wait_idle()
            assert runtime.manager.servers["kb"].connected
            assert runtime.manager.servers["search"] is second
            source.change("mcp.servers", [kb])
            assert follower.wait_idle()
            assert set(runtime.manager.servers) == {"kb"} and not second.connected
        finally:
            runtime.stop()


class _Source:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = dict(values)
        self.observers: list[Any] = []

    def get(self, key: str) -> Any:
        return self.values.get(key, registry.spec(key).default)

    def on_change(self, observer: Any) -> None:
        self.observers.append(observer)

    def change(self, key: str, value: Any) -> None:
        self.values[key] = value
        for observer in self.observers:
            observer()


# -- The tool decision point -------------------------------------------------------------


class TestDecisionPoint:
    def test_personal_mode_offers_every_server(self) -> None:
        assert is_mcp_server_available("search", permissions=None)

    @pytest.mark.parametrize(
        ("permissions", "allowed"),
        [
            (None, False),
            (ANY_ROLE, False),
            (frozenset(), False),
            (frozenset({"spark.mcp.finance"}), False),
            (frozenset({"spark.mcp.search"}), True),
            ({"spark.mcp.search"}, True),
        ],
    )
    def test_server_mode_needs_the_users_own_grant(
        self, server_mode: None, permissions: Any, allowed: bool
    ) -> None:
        assert is_mcp_server_available("search", permissions=permissions) is allowed

    def test_the_manager_asks_the_decision_point(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import spark.tools.registry as tool_registry

        asked: list[str] = []

        def decide(server_: str, **kwargs: Any) -> bool:
            asked.append(server_)
            return server_ == "search"

        monkeypatch.setattr(tool_registry, "is_mcp_server_available", decide)
        manager = MCPManager()
        for name in ("search", "finance"):
            manager.add_server(
                MCPServerConfig(name=name, transport="http", url=f"https://{name}.example.com")
            )
        assert set(manager.servers_for(None)) == {"search"}
        assert set(asked) == {"search", "finance"}
