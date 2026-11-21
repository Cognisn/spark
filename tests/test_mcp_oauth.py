"""OAuth for MCP servers: the token store, the sign-in hook and conduit's client (C14).

Guards under test: the token store keeps conduit's records only in the secrets
store, in parts small enough for any backend, never in a repr or a log; one store
object per server, so conduit's lock is shared by every client of the server and
concurrent refreshes make one refresh request; a server's records are its own even
when conduit's derived names collide; a pending sign-in is used once, by the nonce
of the browser that started it, and expires; conduit's state and redirect checks
refuse a tampered callback; a server conduit refuses shows conduit's reason. The
authorisation server is synthetic (tests.mcp_oauth_helpers).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from conduit.core.backends import TokenSet
from conduit.integrations.konfig import KonfigSecretResolver
from conduit.testing.conformance import TokenStoreSuite
from pydantic import SecretStr

from spark.mcp_integration import oauth
from spark.mcp_integration.client import MCPClient, MCPServerConfig
from spark.mcp_integration.manager import MCPManager
from spark.mcp_integration.servers import ServerEntryError, check_oauth, validate_entry
from tests.mcp_oauth_helpers import ISSUER, MemorySecrets, SyntheticAuthServer

RESOURCE = "https://mcp.example.com/mcp"
REDIRECT = "http://127.0.0.1:22400/mcp/oauth/callback"


def _config(name: str = "docs", **extra: Any) -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="http", url=RESOURCE, auth_type="oauth", timeout=5.0, **extra
    )


def _client(
    secrets: MemorySecrets, auth: SyntheticAuthServer, config: MCPServerConfig | None = None
) -> MCPClient:
    config = config or _config()
    return MCPClient(
        config,
        secrets=KonfigSecretResolver(secrets),
        tokens=oauth.token_store(secrets, config.name),
        http_transport=auth.transport(),
    )


def _tokens(n: int, *, expires: datetime | None = None) -> TokenSet:
    return TokenSet(
        access_token=SecretStr(f"access-{n}-dev-only"),
        refresh_token=SecretStr(f"refresh-{n}-dev-only"),
        token_type=f"Bearer;iss={ISSUER}",
        expires_at=expires,
    )


# -- The token store --------------------------------------------------------------------------


class TestConformance(TokenStoreSuite):
    """conduit's own token store suite, over the secrets store."""

    async def make_store(self) -> Any:
        return oauth.SecretsTokenStore(MemorySecrets(), "conformance")


class TestTokenStore:
    async def test_values_are_split_into_parts_no_longer_than_the_limit(self) -> None:
        secrets = MemorySecrets()
        store = oauth.token_store(secrets, "docs")
        long = TokenSet(access_token=SecretStr("a" * 2500), refresh_token=SecretStr("r" * 900))
        await store.put("mcp:docs", long)
        assert all(len(v) <= oauth.PART_LIMIT for v in secrets.data.values())
        assert len(secrets.data) >= 4  # the head and at least three parts
        assert await store.get("mcp:docs") == long

    async def test_a_new_generation_replaces_the_old_parts(self) -> None:
        secrets = MemorySecrets()
        store = oauth.token_store(secrets, "docs")
        await store.put("mcp:docs", _tokens(1))
        first = set(secrets.data)
        await store.put("mcp:docs", _tokens(2))
        assert await store.get("mcp:docs") == _tokens(2)
        assert not any("access-1" in v or "refresh-1" in v for v in secrets.data.values())
        assert len(secrets.data) == len(first)

    async def test_delete_removes_every_part(self) -> None:
        secrets = MemorySecrets()
        store = oauth.token_store(secrets, "docs")
        await store.put("mcp:docs", _tokens(1))
        await store.delete("mcp:docs")
        assert secrets.data == {}
        assert await store.get("mcp:docs") is None

    async def test_an_unreadable_record_is_a_store_error_naming_no_value(self) -> None:
        secrets = MemorySecrets()
        store = oauth.token_store(secrets, "docs")
        await store.put("mcp:docs", _tokens(1))
        for name in list(secrets.data):
            if name.count(".") == 3:  # a part
                secrets.data[name] = '{"access_token": "access-1-dev-only"'
        with pytest.raises(oauth.TokenStoreError) as caught:
            await store.get("mcp:docs")
        assert "dev-only" not in str(caught.value)

    def test_repr_names_the_server_only(self) -> None:
        store = oauth.token_store(MemorySecrets(), "docs")
        assert repr(store) == "SecretsTokenStore('docs')"

    def test_one_store_per_server_and_secrets_store(self) -> None:
        secrets = MemorySecrets()
        assert oauth.token_store(secrets, "docs") is oauth.token_store(secrets, "docs")
        assert oauth.token_store(secrets, "docs") is not oauth.token_store(secrets, "other")
        assert oauth.token_store(secrets, "docs") is not oauth.token_store(MemorySecrets(), "docs")

    async def test_servers_whose_conduit_names_collide_keep_their_own_records(self) -> None:
        secrets = MemorySecrets()
        one, two = oauth.token_store(secrets, "a-b"), oauth.token_store(secrets, "a_b")
        await one.put("mcp:a_b", _tokens(1))
        assert await two.get("mcp:a_b") is None
        assert one.signed_in() and not two.signed_in()

    async def test_forget_drops_tokens_and_registration(self) -> None:
        secrets = MemorySecrets()
        store = oauth.token_store(secrets, "docs")
        await store.put("mcp:docs", _tokens(1))
        await store.put("mcp:docs:client", _tokens(2))
        store.last_failure = "refresh"
        store.forget()
        assert secrets.data == {}
        assert store.last_failure is None

    async def test_status(self) -> None:
        store = oauth.token_store(MemorySecrets(), "docs")
        assert oauth.oauth_status(store)["state"] == "needs_sign_in"
        await store.put("mcp:docs", _tokens(1))
        assert oauth.oauth_status(store)["state"] == "signed_in"
        store.last_failure = "discovery"
        status = oauth.oauth_status(store)
        assert status["state"] == "failed" and status["reason"] == "discovery"
        assert "discovery metadata" in status["message"]
        store.last_failure = "sign_in_required"
        assert oauth.oauth_status(store)["state"] == "signed_in"


# -- The entry -----------------------------------------------------------------------------


class TestEntry:
    def _entry(self, **extra: Any) -> dict[str, Any]:
        return {"name": "docs", "transport": "http", "url": RESOURCE, "auth_type": "oauth", **extra}

    def test_dynamic_registration_needs_nothing_more(self) -> None:
        checked = validate_entry(self._entry(), server=True)
        assert checked["oauth_client_id"] is None and checked["oauth_scopes"] == []

    def test_scopes_are_kept_as_a_list(self) -> None:
        checked = validate_entry(self._entry(oauth_scopes="tools:read tools:call"), server=True)
        assert checked["oauth_scopes"] == ["tools:read", "tools:call"]

    def test_a_scope_with_a_quote_is_refused(self) -> None:
        with pytest.raises(ServerEntryError, match="scopes"):
            validate_entry(self._entry(oauth_scopes=['bad"scope']), server=True)

    def test_a_secret_needs_the_client_id(self) -> None:
        with pytest.raises(ServerEntryError, match="client id"):
            check_oauth(self._entry(oauth_issuer=ISSUER), secret_set=True)

    def test_a_secret_needs_the_issuer(self) -> None:
        with pytest.raises(ServerEntryError, match="issuer"):
            check_oauth(self._entry(oauth_client_id="spark"), secret_set=True)

    def test_a_secret_with_client_id_and_issuer_is_accepted(self) -> None:
        checked = check_oauth(
            self._entry(oauth_client_id="spark", oauth_issuer=ISSUER), secret_set=True
        )
        assert checked["oauth_issuer"] == ISSUER

    def test_a_plain_http_issuer_is_refused(self) -> None:
        with pytest.raises(ServerEntryError, match="issuer was refused"):
            check_oauth(self._entry(oauth_issuer="http://auth.example.com"), secret_set=False)

    def test_oauth_needs_certificate_checks(self) -> None:
        with pytest.raises(ServerEntryError, match="Verify SSL"):
            validate_entry(self._entry(ssl_verify=False), server=True)

    def test_oauth_needs_an_http_server(self) -> None:
        with pytest.raises(ServerEntryError, match="HTTP or SSE"):
            validate_entry(
                {"name": "docs", "transport": "stdio", "auth_type": "oauth"}, server=False
            )

    def test_settings_carry_conduits_oauth(self) -> None:
        config = _config(
            oauth_client_id="spark",
            oauth_client_secret="client-secret-dev-only",
            oauth_issuer=ISSUER,
        )
        settings, secrets = MCPClient(config).settings()
        auth: Any = settings.auth
        assert auth.type == "oauth" and auth.client_id == "spark" and auth.issuer == ISSUER
        assert auth.client_secret is not None
        assert "client-secret" not in repr(settings) and "client-secret" not in repr(config)


# -- The sign-in ---------------------------------------------------------------------------


async def _browser(started: oauth.SignInStarted, auth: SyntheticAuthServer, **kw: Any) -> str:
    assert started.state == "redirect", started
    return auth.authorise(str(started.authorization_url), **kw)


class TestSignIn:
    async def _start(
        self, sign_ins: oauth.SignIns, client: MCPClient, secrets: MemorySecrets
    ) -> oauth.SignInStarted:
        return await sign_ins.start(
            "docs",
            client.oauth_client,
            REDIRECT,
            store=oauth.token_store(secrets, "docs"),
            actor={"user_guid": "user-1"},
        )

    async def test_round_trip_with_dynamic_registration(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        callback = await _browser(started, auth)
        outcome = await sign_ins.complete(started.nonce, callback)
        assert outcome.signed_in and outcome.server == "docs"
        assert outcome.actor == {"user_guid": "user-1"}
        store = oauth.token_store(secrets, "docs")
        assert store.signed_in() and store.last_failure is None
        assert (await store.get("mcp:docs")).access_token.get_secret_value() in auth.issued

    async def test_pre_registered_client_with_secret(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE, registration=False)
        auth.pre_register("spark", "client-secret-dev-only")
        config = _config(
            oauth_client_id="spark",
            oauth_client_secret="client-secret-dev-only",
            oauth_issuer=ISSUER,
        )
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth, config), secrets)
        outcome = await sign_ins.complete(started.nonce, await _browser(started, auth))
        assert outcome.signed_in
        assert auth.token_requests[-1]["client_secret"] == "client-secret-dev-only"

    async def test_a_callback_without_the_nonce_is_refused_and_changes_nothing(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        callback = await _browser(started, auth)
        for nonce in (None, "", "forged-nonce"):
            outcome = await sign_ins.complete(nonce, callback)
            assert not outcome.signed_in and outcome.reason == "unknown_sign_in"
            assert outcome.server is None
        assert sign_ins.pending_servers() == ["docs"]
        assert (await sign_ins.complete(started.nonce, callback)).signed_in

    async def test_a_pending_sign_in_is_used_once(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        callback = await _browser(started, auth)
        assert (await sign_ins.complete(started.nonce, callback)).signed_in
        again = await sign_ins.complete(started.nonce, callback)
        assert not again.signed_in and again.reason == "unknown_sign_in"

    async def test_an_expired_sign_in_is_refused(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        now = [1000.0]
        sign_ins = oauth.SignIns(clock=lambda: now[0])
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        callback = await _browser(started, auth)
        now[0] += oauth.SIGN_IN_SECONDS + 1
        outcome = await sign_ins.complete(started.nonce, callback)
        assert not outcome.signed_in and outcome.reason == "expired"
        assert not oauth.token_store(secrets, "docs").signed_in()

    async def test_a_tampered_state_is_refused_by_conduit(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        callback = await _browser(started, auth)
        parts = urlsplit(callback)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query["state"] = "tampered-state"
        outcome = await sign_ins.complete(started.nonce, f"{REDIRECT}?{urlencode(query)}")
        assert not outcome.signed_in and outcome.reason == "state_mismatch"
        assert not oauth.token_store(secrets, "docs").signed_in()
        assert oauth.token_store(secrets, "docs").last_failure == "state_mismatch"

    async def test_a_callback_at_another_address_is_refused_by_conduit(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        callback = await _browser(started, auth)
        moved = callback.replace("http://127.0.0.1:22400", "https://elsewhere.example.net")
        outcome = await sign_ins.complete(started.nonce, moved)
        assert not outcome.signed_in and outcome.reason == "redirect_mismatch"

    async def test_a_denied_sign_in_reports_the_denial(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        started = await self._start(sign_ins, _client(secrets, auth), secrets)
        outcome = await sign_ins.complete(started.nonce, await _browser(started, auth, deny=True))
        assert not outcome.signed_in and outcome.reason == "authorisation_denied"

    async def test_a_server_without_discovery_metadata_shows_conduits_reason(self) -> None:
        secrets = MemorySecrets()
        auth = SyntheticAuthServer(RESOURCE, publish_metadata=False)
        started = await self._start(oauth.SignIns(), _client(secrets, auth), secrets)
        assert started.state == "failed" and started.reason == "discovery"
        assert oauth.token_store(secrets, "docs").last_failure == "discovery"

    async def test_a_pinned_issuer_refuses_another(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        config = _config(oauth_issuer="https://other.example.org")
        started = await self._start(oauth.SignIns(), _client(secrets, auth, config), secrets)
        assert started.state == "failed" and started.reason == "issuer_mismatch"

    async def test_starting_again_cancels_the_earlier_sign_in(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        sign_ins = oauth.SignIns()
        first = await self._start(sign_ins, _client(secrets, auth), secrets)
        second = await self._start(sign_ins, _client(secrets, auth), secrets)
        stale = await sign_ins.complete(first.nonce, await _browser(first, auth))
        assert not stale.signed_in and stale.reason == "unknown_sign_in"
        assert (await sign_ins.complete(second.nonce, await _browser(second, auth))).signed_in

    async def test_an_already_signed_in_server_needs_no_redirect(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await oauth.token_store(secrets, "docs").put("mcp:docs", _tokens(1))
        started = await self._start(oauth.SignIns(), _client(secrets, auth), secrets)
        assert started.state == "signed_in"


# -- Refresh, the lock, sign-out -----------------------------------------------------------


async def _signed_in(secrets: MemorySecrets, auth: SyntheticAuthServer) -> None:
    sign_ins = oauth.SignIns()
    client = _client(secrets, auth)
    started = await sign_ins.start(
        "docs", client.oauth_client, REDIRECT, store=oauth.token_store(secrets, "docs")
    )
    assert (await sign_ins.complete(started.nonce, await _browser(started, auth))).signed_in


async def _expire(secrets: MemorySecrets) -> TokenSet:
    store = oauth.token_store(secrets, "docs")
    held = await store.get("mcp:docs")
    assert held is not None
    stale = held.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(minutes=5)})
    await store.put("mcp:docs", stale)
    return stale


class TestRefresh:
    async def test_an_expired_token_is_refreshed_and_rotated(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        stale = await _expire(secrets)
        token = await _client(secrets, auth).oauth_client(None).access_token(interactive=False)
        assert token.get_secret_value() != stale.access_token.get_secret_value()
        assert auth.refreshes == 1
        held = await oauth.token_store(secrets, "docs").get("mcp:docs")
        assert held is not None and held.refresh_token != stale.refresh_token

    async def test_concurrent_clients_of_a_server_share_one_refresh(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        await _expire(secrets)
        clients = [_client(secrets, auth).oauth_client(None) for _ in range(5)]
        tokens = await asyncio.gather(*(c.access_token(interactive=False) for c in clients))
        sent = [r for r in auth.token_requests if r.get("grant_type") == "refresh_token"]
        assert len(sent) == 1  # one refresh request, not one refused per extra client
        assert auth.refreshes == 1
        assert len({t.get_secret_value() for t in tokens}) == 1

    async def test_a_refused_refresh_needs_a_new_sign_in(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        await _expire(secrets)
        auth.refresh_tokens.clear()  # the authorisation server revoked them
        from conduit.mcp import McpAuthError

        with pytest.raises(McpAuthError) as caught:
            await _client(secrets, auth).oauth_client(None).access_token(interactive=False)
        assert caught.value.reason.value == "sign_in_required"
        assert not oauth.token_store(secrets, "docs").signed_in()

    async def test_sign_out_removes_the_tokens(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        await _signed_in(secrets, auth)
        await _client(secrets, auth).oauth_client(None).sign_out()
        assert not oauth.token_store(secrets, "docs").signed_in()

    async def test_a_connection_never_signs_in(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        client = _client(secrets, auth)
        assert not await client.connect()
        assert client.last_failure == "sign_in_required"
        assert auth.codes == {} and auth.token_requests == []

    async def test_the_manager_gives_every_client_of_a_server_one_store(self) -> None:
        secrets = MemorySecrets()
        manager = MCPManager(secrets=KonfigSecretResolver(secrets))
        assert manager.token_secrets is secrets
        assert manager.token_store("docs") is manager.token_store("docs")
        one: Any = manager.new_client(_config())
        two: Any = manager.new_client(_config())
        assert one._tokens is two._tokens is manager.token_store("docs")


class TestNothingLeaks:
    async def test_no_token_code_or_secret_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE, registration=False)
        auth.pre_register("spark", "client-secret-dev-only")
        config = _config(
            oauth_client_id="spark",
            oauth_client_secret="client-secret-dev-only",
            oauth_issuer=ISSUER,
        )
        sign_ins = oauth.SignIns()
        client = _client(secrets, auth, config)
        started = await sign_ins.start(
            "docs", client.oauth_client, REDIRECT, store=oauth.token_store(secrets, "docs")
        )
        callback = await _browser(started, auth)
        assert (await sign_ins.complete(started.nonce, callback)).signed_in
        await _expire(secrets)
        await _client(secrets, auth, config).oauth_client(None).access_token(interactive=False)
        state = parse_qs(urlsplit(callback).query)["state"][0]
        text = caplog.text + "".join(repr(r.args) for r in caplog.records)
        for value in [*auth.issued, "client-secret-dev-only", state, str(started.nonce)]:
            assert value not in text
        assert "dev-only" not in repr(started) + repr(sign_ins)

    async def test_custom_headers_never_reach_the_authorisation_server(self) -> None:
        secrets, auth = MemorySecrets(), SyntheticAuthServer(RESOURCE)
        config = _config(custom_headers={"X-Key": "header-dev-only"})
        sign_ins = oauth.SignIns()
        client = _client(secrets, auth, config)
        started = await sign_ins.start(
            "docs", client.oauth_client, REDIRECT, store=oauth.token_store(secrets, "docs")
        )
        assert (await sign_ins.complete(started.nonce, await _browser(started, auth))).signed_in
        to_issuer = [h for url, h in auth.requests if url.startswith(ISSUER)]
        to_server = [h for url, h in auth.requests if url.startswith("https://mcp.example.com")]
        assert to_issuer and all("x-key" not in h for h in to_issuer)
        assert to_server and all(h.get("x-key") == "header-dev-only" for h in to_server)
