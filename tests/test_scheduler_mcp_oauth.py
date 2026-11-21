"""Autonomous actions and OAuth MCP servers: the executor's MCP manager (Q68).

Guards under test: the action executor builds its MCP manager as the web
application does (SPARK's resolver over konfig's secrets store, and that store
for the OAuth tokens), so a server an administrator signed in to connects for an
action, in personal mode from ``config.yaml`` and in server mode from the
instance settings; an action reaches only the servers its owner holds
``spark.mcp.<server>`` for, read at run time; a server that is not signed in, or
whose refresh fails, is logged by name and failure class only and the action
carries on without it; no token is ever logged.

Refresh across processes: conduit's lock is per token store object (and loop), so
the web process and the scheduler may refresh one token at once. Two token store
objects over one secrets store stand for the two processes, with the refresh
requests held so each interleaving is forced. Every interleaving ends with a
working token, or conduit's ``invalid_grant`` fallback asking for a new sign-in,
and the stored record is always readable.

The MCP server is the local stub (tests.mcp_stub_server) and the authorisation
server is synthetic (tests.mcp_oauth_helpers): nothing leaves this machine.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from conduit.integrations.konfig import KonfigSecretResolver
from conduit.mcp import McpAuthError

from spark import mode
from spark.instance_settings import repository as settings_repository
from spark.mcp_integration import manager as manager_module
from spark.mcp_integration import oauth
from spark.mcp_integration.client import MCPClient, MCPServerConfig
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.scheduler.executor import ActionExecutor
from spark.tools.registry import mcp_permission_name
from tests.mcp_oauth_helpers import MemorySecrets, SyntheticAuthServer
from tests.mcp_stub_server import StubServer, serving
from tests.test_mcp_oauth import RESOURCE, _browser, _expire, _signed_in

REDIRECT = "http://127.0.0.1:22300/mcp/oauth/callback"
OWNER = "owner-guid-1"
ACTIONS = "spark.actions"


class _Settings:
    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return default


class Ctx:
    """The executor's application context: konfig's settings and secrets store."""

    def __init__(self, secrets: MemorySecrets) -> None:
        self.settings = _Settings()
        self.secrets = secrets


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    mode.reset()


@pytest.fixture(scope="module")
def stub() -> Iterator[StubServer]:
    with serving() as server_:
        yield server_


@pytest.fixture
def auth(stub: StubServer) -> SyntheticAuthServer:
    return SyntheticAuthServer(stub.http_url)


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch, auth: SyntheticAuthServer) -> None:
    """Every MCP client the manager makes reaches the synthetic authorisation server.

    The manager's own client construction (its resolver and token store) is kept;
    only the network transport is replaced, as the mcp-oauth tests do.
    """

    class Routed(MCPClient):
        def __init__(self, config: MCPServerConfig, **kw: Any) -> None:
            kw.setdefault("http_transport", auth.transport(httpx2.AsyncHTTPTransport()))
            super().__init__(config, **kw)

    monkeypatch.setattr(manager_module, "MCPClient", Routed)


def _entry(stub: StubServer, name: str, auth_type: str = "oauth", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "transport": "http",
        "url": stub.http_url,
        "auth_type": auth_type,
        "enabled": True,
        "timeout": 10,
        **extra,
    }


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.yaml"
    import spark.core.application as app_mod

    monkeypatch.setattr(app_mod, "_get_config_path", lambda: path)
    return path


def _write_servers(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(yaml.safe_dump({"mcp": {"servers": entries}}), encoding="utf-8", newline="\n")


def _sign_in(secrets: MemorySecrets, auth: SyntheticAuthServer, stub: StubServer) -> None:
    """An administrator signs in to server ``docs`` (as the web's settings page does)."""

    async def run() -> None:
        config = MCPServerConfig(
            name="docs", transport="http", url=stub.http_url, auth_type="oauth", timeout=10
        )
        client = MCPClient(
            config,
            secrets=KonfigSecretResolver(secrets),
            tokens=oauth.token_store(secrets, "docs"),
            http_transport=auth.transport(),
        )
        sign_ins = oauth.SignIns()
        started = await sign_ins.start(
            "docs", client.oauth_client, REDIRECT, store=oauth.token_store(secrets, "docs")
        )
        outcome = await sign_ins.complete(started.nonce, await _browser(started, auth))
        assert outcome.signed_in

    asyncio.run(run())


def _issued_access(auth: SyntheticAuthServer) -> set[str]:
    return {v for v in auth.issued if v.startswith("access-")}


def _bearers(stub: StubServer) -> list[str]:
    return [h["authorization"] for h in stub.headers if "authorization" in h]


class _Run:
    """An executor's MCP set-up, its tools and its calls, cleaned up afterwards."""

    def __init__(self, executor: ActionExecutor) -> None:
        self.executor = executor

    def __enter__(self) -> ActionExecutor:
        self.executor._init_mcp()
        return self.executor

    def __exit__(self, *exc: Any) -> None:
        self.executor._cleanup_mcp()


def _personal(secrets: MemorySecrets) -> _Run:
    return _Run(ActionExecutor(Ctx(secrets), "daemon-1", object(), user_guid=OWNER))  # type: ignore[arg-type]


def _tool_names(executor: ActionExecutor) -> set[str]:
    return {t["name"] for t in executor._get_tools()}


# -- Personal mode: config.yaml ------------------------------------------------------------


class TestPersonal:
    def test_a_signed_in_oauth_server_connects_for_an_action(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        config_file: Path,
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs")])
        _sign_in(secrets, auth, stub)
        stub.headers.clear()
        with _personal(secrets) as executor:
            assert executor._mcp_manager is not None
            assert executor._mcp_manager.servers["docs"].connected
            assert "echo" in _tool_names(executor)
            assert executor._execute_tool("echo", {"text": "hi"}) == "echo: hi"
        bearers = _bearers(stub)
        assert bearers and all(b.removeprefix("Bearer ") in _issued_access(auth) for b in bearers)

    def test_the_manager_shares_the_instance_token_store(
        self, routed: None, stub: StubServer, config_file: Path
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs")])
        with _personal(secrets) as executor:
            manager = executor._mcp_manager
            assert manager is not None
            assert manager.token_secrets is secrets
            client: Any = manager.servers["docs"]
            assert client._tokens is oauth.token_store(secrets, "docs")

    def test_a_credential_reference_is_resolved_through_the_secrets_store(
        self, routed: None, stub: StubServer, config_file: Path
    ) -> None:
        secrets = MemorySecrets()
        secrets.set("plain_token", "bearer-ref-dev-only")
        _write_servers(
            config_file, [_entry(stub, "plain", "bearer", auth_token="secret://plain_token")]
        )
        stub.headers.clear()
        with _personal(secrets) as executor:
            assert executor._execute_tool("echo", {"text": "r"}) == "echo: r"
        assert "Bearer bearer-ref-dev-only" in _bearers(stub)

    def test_an_expired_token_is_refreshed_and_stored(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        config_file: Path,
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs")])
        _sign_in(secrets, auth, stub)

        async def expire() -> Any:
            store = oauth.token_store(secrets, "docs")
            held = await store.get("mcp:docs")
            from datetime import UTC, datetime, timedelta

            stale = held.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(minutes=5)})
            await store.put("mcp:docs", stale)
            return stale

        stale = asyncio.run(expire())
        with _personal(secrets) as executor:
            assert executor._execute_tool("echo", {"text": "x"}) == "echo: x"
        assert auth.refreshes == 1
        held = asyncio.run(oauth.token_store(secrets, "docs").get("mcp:docs"))
        assert held is not None and held.refresh_token != stale.refresh_token

    def test_a_server_not_signed_in_is_left_out_and_the_action_carries_on(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        config_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs"), _entry(stub, "plain", "none")])
        with _personal(secrets) as executor:
            assert executor._mcp_manager is not None
            assert not executor._mcp_manager.servers["docs"].connected
            assert executor._mcp_manager.servers["plain"].connected
            assert executor._execute_tool("echo", {"text": "on"}) == "echo: on"
        assert auth.codes == {} and auth.token_requests == []  # never signs in
        assert _left_out(caplog, "docs", "sign_in_required")

    def test_a_refused_refresh_is_logged_by_class_and_the_action_carries_on(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        config_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs"), _entry(stub, "plain", "none")])
        _sign_in(secrets, auth, stub)
        asyncio.run(_expire(secrets))
        auth.refresh_tokens.clear()  # the authorisation server revoked them
        caplog.set_level(logging.DEBUG)
        with _personal(secrets) as executor:
            assert executor._mcp_manager is not None
            assert not executor._mcp_manager.servers["docs"].connected
            assert executor._execute_tool("echo", {"text": "on"}) == "echo: on"
        assert not oauth.token_store(secrets, "docs").signed_in()
        assert _left_out(caplog, "docs", "sign_in_required")
        _assert_nothing_logged(caplog, auth)

    def test_a_failing_token_endpoint_is_logged_by_class(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        config_file: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs"), _entry(stub, "plain", "none")])
        _sign_in(secrets, auth, stub)
        asyncio.run(_expire(secrets))
        monkeypatch.setattr(
            auth, "_token", lambda form: httpx2.Response(503, json={"error": "unavailable"})
        )
        caplog.set_level(logging.DEBUG)
        with _personal(secrets) as executor:
            assert executor._mcp_manager is not None
            assert not executor._mcp_manager.servers["docs"].connected
            assert executor._execute_tool("echo", {"text": "on"}) == "echo: on"
        assert _left_out(caplog, "docs", "refresh")
        assert oauth.token_store(secrets, "docs").signed_in()  # kept for the next try
        _assert_nothing_logged(caplog, auth)

    def test_no_token_is_logged_by_a_run(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        config_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "docs")])
        _sign_in(secrets, auth, stub)
        asyncio.run(_expire(secrets))
        caplog.set_level(logging.DEBUG)
        with _personal(secrets) as executor:
            executor._get_tools()
            assert executor._execute_tool("echo", {"text": "x"}) == "echo: x"
        assert auth.refreshes == 1
        _assert_nothing_logged(caplog, auth)


def _left_out(caplog: pytest.LogCaptureFixture, server: str, failure: str) -> bool:
    """Whether the executor logged ``server`` as left out, with conduit's failure class."""
    return any(
        r.name == "spark.scheduler.executor"
        and r.getMessage() == f"Executor MCP: {server} not connected ({failure}); "
        "the action runs without it"
        for r in caplog.records
    )


def _assert_nothing_logged(caplog: pytest.LogCaptureFixture, auth: SyntheticAuthServer) -> None:
    text = caplog.text + "".join(repr(r.args) for r in caplog.records)
    for value in auth.issued:
        assert value not in text


# -- Server mode: the instance settings, the owner's permissions ---------------------------


def _server_mode() -> None:
    mode.configure(
        RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://db.example.com/x")
        )
    )


def _server_run(secrets: MemorySecrets, db: Any, permissions: Any) -> _Run:
    return _Run(
        ActionExecutor(Ctx(secrets), "worker-1", db, permissions=permissions, user_guid=OWNER)
    )


def _store_servers(db: Any, entries: list[dict[str, Any]]) -> None:
    db.call(settings_repository.save_instance, "mcp.servers", entries, actor="test")


@pytest.fixture
def server_db(spark_db_settings: Any) -> Iterator[Any]:
    """The bridge to a fresh SPARK database: SQLite, and (marked postgres) PostgreSQL."""
    from spark.db.standalone import open_standalone

    _server_mode()
    handle = open_standalone(spark_db_settings, auto_migrate=True)
    try:
        yield handle.sync_db
    finally:
        handle.close()


class TestServer:
    def test_the_servers_come_from_the_instance_settings(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        server_db: Any,
        config_file: Path,
    ) -> None:
        secrets = MemorySecrets()
        _write_servers(config_file, [_entry(stub, "from-yaml", "none")])  # ignored on a server
        _store_servers(server_db, [_entry(stub, "docs", roles=["user"])])
        _sign_in(secrets, auth, stub)
        stub.headers.clear()
        granted = {ACTIONS, mcp_permission_name("docs")}
        with _server_run(secrets, server_db, granted) as executor:
            assert executor._mcp_manager is not None
            assert set(executor._mcp_manager.servers) == {"docs"}
            assert "echo" in _tool_names(executor)
            assert executor._execute_tool("echo", {"text": "srv"}) == "echo: srv"
        bearers = _bearers(stub)
        assert bearers and all(b.removeprefix("Bearer ") in _issued_access(auth) for b in bearers)

    def test_credentials_come_from_the_secrets_store(
        self, routed: None, stub: StubServer, server_db: Any
    ) -> None:
        secrets = MemorySecrets()
        secrets.set("mcp_server_keyed_auth_token", "bearer-dev-only")
        _store_servers(server_db, [_entry(stub, "keyed", "bearer", roles=["user"])])
        stub.headers.clear()
        granted = {ACTIONS, mcp_permission_name("keyed")}
        with _server_run(secrets, server_db, granted) as executor:
            assert executor._execute_tool("echo", {"text": "k"}) == "echo: k"
        assert "Bearer bearer-dev-only" in _bearers(stub)

    def test_an_owner_without_the_server_permission_does_not_reach_it(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        server_db: Any,
    ) -> None:
        secrets = MemorySecrets()
        _store_servers(server_db, [_entry(stub, "docs", roles=["user"])])
        _sign_in(secrets, auth, stub)
        stub.headers.clear()
        with _server_run(secrets, server_db, {ACTIONS, mcp_permission_name("other")}) as executor:
            assert "echo" not in _tool_names(executor)
            text = executor._execute_tool("echo", {"text": "no"})
            assert "not available" in text and "echo: no" not in text
        assert not any("tools/call" in str(h) for h in stub.headers)
        assert all("echo" not in str(h) for h in stub.headers)

    def test_an_owner_with_no_permissions_reaches_no_server(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        server_db: Any,
    ) -> None:
        secrets = MemorySecrets()
        _store_servers(server_db, [_entry(stub, "docs", roles=["user"])])
        _sign_in(secrets, auth, stub)
        with _server_run(secrets, server_db, None) as executor:
            assert "echo" not in _tool_names(executor)
            assert "echo: no" not in executor._execute_tool("echo", {"text": "no"})

    def test_the_permission_is_read_for_each_run(
        self,
        routed: None,
        auth: SyntheticAuthServer,
        stub: StubServer,
        server_db: Any,
    ) -> None:
        secrets = MemorySecrets()
        _store_servers(server_db, [_entry(stub, "docs", roles=["user"])])
        _sign_in(secrets, auth, stub)
        with _server_run(secrets, server_db, {ACTIONS, mcp_permission_name("docs")}) as first:
            assert first._execute_tool("echo", {"text": "a"}) == "echo: a"
        with _server_run(secrets, server_db, {ACTIONS}) as second:  # the grant was removed
            assert "echo: b" not in second._execute_tool("echo", {"text": "b"})

    def test_a_server_not_signed_in_is_left_out(
        self,
        routed: None,
        stub: StubServer,
        server_db: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        secrets = MemorySecrets()
        secrets.set("mcp_server_plain_auth_token", "bearer-dev-only")
        _store_servers(
            server_db,
            [_entry(stub, "docs", roles=["user"]), _entry(stub, "plain", "bearer", roles=["user"])],
        )
        granted = {ACTIONS, mcp_permission_name("docs"), mcp_permission_name("plain")}
        with _server_run(secrets, server_db, granted) as executor:
            assert executor._mcp_manager is not None
            assert not executor._mcp_manager.servers["docs"].connected
            assert executor._execute_tool("echo", {"text": "p"}) == "echo: p"
        assert _left_out(caplog, "docs", "sign_in_required")
        assert "bearer-dev-only" not in caplog.text

    def test_no_servers_stored_means_no_manager(self, server_db: Any) -> None:
        with _server_run(MemorySecrets(), server_db, {ACTIONS}) as executor:
            assert executor._mcp_manager is None


# -- Refresh across processes ----------------------------------------------------------------


class _Gate(httpx2.AsyncBaseTransport):
    """Holds this process's refresh request before and after the authorisation server."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner
        self.arrived = asyncio.Event()
        self.answered = asyncio.Event()
        self.before: Callable[[], Awaitable[Any]] | None = None
        self.after: Callable[[], Awaitable[Any]] | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        refresh = b"grant_type=refresh_token" in (request.content or b"")
        if refresh:
            self.arrived.set()
            if self.before is not None:
                await asyncio.wait_for(self.before(), 10)
        response = await self._inner.handle_async_request(request)
        if refresh:
            self.answered.set()
            if self.after is not None:
                await asyncio.wait_for(self.after(), 10)
        return response


class _Process:
    """One process's view: its own token store object (and so its own conduit lock)."""

    def __init__(self, secrets: Any, auth: SyntheticAuthServer, *, cached: bool) -> None:
        self.store = (
            oauth.token_store(secrets, "docs")
            if cached
            else oauth.SecretsTokenStore(secrets, "docs")
        )
        self.gate = _Gate(auth.transport())
        config = MCPServerConfig(
            name="docs", transport="http", url=RESOURCE, auth_type="oauth", timeout=5.0
        )
        self.client = MCPClient(
            config,
            secrets=KonfigSecretResolver(secrets),
            tokens=self.store,
            http_transport=self.gate,
        )
        self.task: asyncio.Task[Any] | None = None

    def start(self) -> asyncio.Task[Any]:
        self.task = asyncio.ensure_future(
            self.client.oauth_client(None).access_token(interactive=False)
        )
        return self.task

    async def done(self) -> None:
        assert self.task is not None
        await asyncio.wait({self.task})


class _HeldDeletes:
    """The second process's view of the shared secrets store, its deletes held on request."""

    def __init__(self, secrets: MemorySecrets) -> None:
        self._secrets = secrets
        self.hold = threading.Event()
        self.release = threading.Event()
        self.held = threading.Event()

    def get(self, key: str) -> str | None:
        return self._secrets.get(key)

    def set(self, key: str, value: str) -> None:
        self._secrets.set(key, value)

    def delete(self, key: str) -> None:
        if self.hold.is_set() and not self.held.is_set():
            self.held.set()
            assert self.release.wait(10)
        self._secrets.delete(key)


async def _readable(secrets: Any) -> Any:
    """The stored tokens as a third process reads them (raises if corrupted)."""
    return await oauth.SecretsTokenStore(secrets, "docs").get("mcp:docs")


def _leftovers(secrets: MemorySecrets, auth: SyntheticAuthServer) -> list[str]:
    """Token parts left in the secrets store that no head names."""
    heads = {}
    for name, raw in secrets.data.items():
        if name.count(".") == 1:
            import json

            heads[name] = json.loads(raw)
    named = {
        f"{name}.{head['generation']}.{i}"
        for name, head in heads.items()
        for i in range(head["parts"])
    }
    return [n for n in secrets.data if n.count(".") == 3 and n not in named]


class TestRefreshAcrossProcesses:
    async def test_the_one_refused_uses_the_others_rotated_token(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        await _expire(secrets)
        web, worker = _Process(secrets, auth, cached=True), _Process(secrets, auth, cached=False)
        web.gate.before = worker.gate.arrived.wait  # both read the stale token first
        worker.gate.before = web.done  # the web process has stored its new tokens
        a, b = web.start(), worker.start()
        tokens = await asyncio.gather(a, b)
        refreshes = [r for r in auth.token_requests if r.get("grant_type") == "refresh_token"]
        assert len(refreshes) == 2 and auth.refreshes == 1  # a real race; one granted
        held = await _readable(secrets)
        assert held is not None
        assert {t.get_secret_value() for t in tokens} == {held.access_token.get_secret_value()}
        assert held.access_token.get_secret_value() in auth.issued
        assert _leftovers(secrets, auth) == []

    async def test_refused_before_the_other_stores_asks_for_a_sign_in_and_keeps_the_new_tokens(
        self,
    ) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        await _expire(secrets)
        web, worker = _Process(secrets, auth, cached=True), _Process(secrets, auth, cached=False)
        web.gate.before = worker.gate.arrived.wait
        web.gate.after = worker.done  # the web's answer is held until the worker gave up
        worker.gate.before = web.gate.answered.wait  # the web's refresh token is spent
        a, b = web.start(), worker.start()
        results = await asyncio.gather(a, b, return_exceptions=True)
        assert isinstance(results[1], McpAuthError)
        assert results[1].reason.value == "sign_in_required"
        assert not isinstance(results[0], BaseException)
        held = await _readable(secrets)
        assert held is not None  # the web's new tokens were stored after the worker's delete
        assert held.access_token.get_secret_value() == results[0].get_secret_value()
        assert _leftovers(secrets, auth) == []
        # The next run, in either process, uses them without a refresh.
        again = await _Process(secrets, auth, cached=False).start()
        assert again.get_secret_value() == results[0].get_secret_value()
        assert auth.refreshes == 1

    async def test_stored_while_the_other_deletes_ends_signed_out_never_corrupted(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        await _expire(secrets)
        view = _HeldDeletes(secrets)
        web, worker = _Process(secrets, auth, cached=True), _Process(view, auth, cached=False)
        web.gate.before = worker.gate.arrived.wait

        async def held() -> None:
            while not view.held.is_set():
                await asyncio.sleep(0.01)

        web.gate.after = held  # the worker has read the stale record and is deleting it
        worker.gate.before = web.gate.answered.wait
        view.hold.set()

        async def release() -> None:
            await web.done()
            view.release.set()

        a, b = web.start(), worker.start()
        results = await asyncio.gather(a, b, release(), return_exceptions=True)
        assert not isinstance(results[0], BaseException)
        assert isinstance(results[1], McpAuthError)
        assert results[1].reason.value == "sign_in_required"
        # The worker's delete removed the head the web had just written: the record
        # reads as none (sign in again), never as a mixture or an error. The web's new
        # parts are left unnamed in the secrets store (see the module and the decision).
        assert await _readable(secrets) is None
        assert not oauth.token_store(secrets, "docs").signed_in()
