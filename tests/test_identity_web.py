"""Server-mode sign-in through warden: cookies, server-side refresh, CSRF, Origin and API keys.

Every test runs the real app on a PostgreSQL clone at head (warden's chain
included) with synthetic users created through warden. Guards under test:

* the access and refresh cookies carry exactly HttpOnly, Secure, SameSite=Strict,
  and the refresh cookie is scoped to the refresh path only;
* a request without a live access token never reaches an endpoint, and never
  falls back to the process's user guid;
* an expired access token is refreshed server-side through the refresh path,
  and nothing outside the refresh path ever reads the refresh cookie;
* every state-changing request needs the CSRF token bound to its warden
  session; Origin (and Host) are pinned to server.public_base_url, streams
  included;
* an API key authenticates as a bearer, limited to its scopes, needs no CSRF
  token, and may not be mixed with a session cookie.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from spark.identity.permissions import ADMINISTRATOR, API_KEYS, USER, role_permissions
from spark.identity.web import (
    ACCESS_COOKIE,
    CHALLENGE_COOKIE,
    REFRESH_COOKIE,
    REFRESH_PATH,
    SESSION_HEADER,
)
from spark.mode import DatabaseSettings, Dialect
from spark.web.security import CSRF_HEADER, CSRF_REFUSED, ORIGIN_REFUSED, OriginPolicy
from tests.identity_web_helpers import (
    PASSWORD,
    PUBLIC,
    MemorySecrets,
    another_client,
    attributes,
    claims,
    cookie_header,
    make_user,
    runtime,
    server_client,
    set_cookie_headers,
    sign_in,
    write_config,
)

pytestmark = pytest.mark.postgres

NAVIGATE = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}
FETCH = {"Accept": "application/json"}
ADA = "ada@example.com"
GRACE = "grace@example.com"


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
def secrets() -> MemorySecrets:
    return MemorySecrets()


@pytest.fixture
def client(
    tmp_path: Path, server_db: DatabaseSettings, secrets: MemorySecrets
) -> Iterator[TestClient]:
    with server_client(write_config(tmp_path / "config.yaml"), server_db, secrets) as test_client:
        yield test_client


@pytest.fixture
def ada(client: TestClient) -> TestClient:
    make_user(client, ADA)
    resp = sign_in(client, ADA)
    assert resp.status_code == 303, resp.text
    return client


def _expired_access_token(client: TestClient) -> str:
    """A validly signed access token for the client's own session, expired an hour ago."""
    current = claims(client.cookies.get(ACCESS_COOKIE) or "")
    sessions = runtime(client).identity.sessions

    async def issue() -> str:
        token, _ = await sessions.issue_access_token(
            uuid.UUID(current["sub"]),
            uuid.UUID(current["sid"]),
            current["amr"],
            now=time.time() - 3600,
        )
        return token

    token: str = client.portal.call(issue)  # type: ignore[union-attr]
    return token


def _replace_access(client: TestClient, token: str) -> None:
    client.cookies.delete(ACCESS_COOKIE)
    client.cookies.set(ACCESS_COOKIE, token, domain="spark.example.com", path="/")


def _conversations(client: TestClient) -> None:
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver

    app = client.app
    app.state.conversation_manager = ConversationManager(  # type: ignore[attr-defined]
        app.state.sync_db, MagicMock(), ContextLimitResolver(None)  # type: ignore[attr-defined]
    )


# -- Sign-in and the cookies --------------------------------------------------------------


class TestSignIn:
    def test_sets_both_cookies_with_the_exact_attributes(self, client: TestClient) -> None:
        make_user(client, ADA)
        resp = sign_in(client, ADA)
        assert resp.status_code == 303
        access = attributes(cookie_header(resp, ACCESS_COOKIE))
        refresh = attributes(cookie_header(resp, REFRESH_COOKIE))
        for attrs in (access, refresh):
            assert "httponly" in attrs
            assert "secure" in attrs
            assert attrs["samesite"].lower() == "strict"
            assert "domain" not in attrs
        assert ACCESS_COOKIE.startswith("__Host-")
        assert access["path"] == "/"
        assert REFRESH_COOKIE.startswith("__Secure-")
        assert refresh["path"] == REFRESH_PATH
        assert resp.headers["cache-control"] == "no-store"

    def test_signed_in_user_reaches_pages(self, ada: TestClient) -> None:
        resp = ada.get("/auth/session", headers=FETCH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == ADA
        assert set(body["permissions"]) == set(role_permissions(USER))

    def test_wrong_password_sets_no_session(self, client: TestClient) -> None:
        make_user(client, ADA)
        resp = sign_in(client, ADA, "wrong-password-dev-only")
        assert resp.status_code == 401
        assert not any(
            h.startswith((ACCESS_COOKIE, REFRESH_COOKIE)) for h in set_cookie_headers(resp)
        )

    def test_unknown_user_gets_the_same_refusal(self, client: TestClient) -> None:
        make_user(client, ADA)
        wrong = sign_in(client, ADA, "wrong-password-dev-only")
        unknown = sign_in(client, "nobody@example.com")
        assert unknown.status_code == wrong.status_code == 401
        assert "Sign-in failed" in unknown.text and "Sign-in failed" in wrong.text

    def test_sign_in_post_needs_its_form_token(self, client: TestClient) -> None:
        make_user(client, ADA)
        resp = client.post(
            "/auth/sign-in",
            data={"email": ADA, "password": PASSWORD},
            headers={"Origin": PUBLIC},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_next_is_kept_when_same_origin_and_dropped_otherwise(self, client: TestClient) -> None:
        make_user(client, ADA)
        for target, expected in (
            ("/conversations?x=1", "/conversations?x=1"),
            ("https://example.org/", "/"),
            ("//example.org/", "/"),
            ("/\\example.org", "/"),
        ):
            client.cookies.clear()
            page = client.get("/auth/sign-in", params={"next": target})
            import re

            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
            assert token
            resp = client.post(
                "/auth/sign-in",
                data={
                    "email": ADA,
                    "password": PASSWORD,
                    "csrf_token": token.group(1),
                    "next": target,
                },
                headers={"Origin": PUBLIC},
                follow_redirects=False,
            )
            assert resp.headers["location"] == expected, target

    def test_an_mfa_challenge_goes_to_the_challenge_screen(self, client: TestClient) -> None:
        make_user(client, ADA)
        identity = runtime(client).identity
        client.portal.call(lambda: identity.policy.update(actor="test", require_mfa=True))  # type: ignore[union-attr]
        resp = sign_in(client, ADA)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/challenge"
        challenge = attributes(cookie_header(resp, CHALLENGE_COOKIE))
        assert "httponly" in challenge and "secure" in challenge
        assert challenge["path"] == "/auth/challenge"
        assert not any(h.startswith(ACCESS_COOKIE) for h in set_cookie_headers(resp))
        page = client.get("/auth/challenge")
        assert page.status_code == 200
        assert "second factor" in page.text.lower()


# -- Requests without a session -----------------------------------------------------------


class TestUnauthenticated:
    def test_navigation_goes_to_sign_in_with_next(self, client: TestClient) -> None:
        resp = client.get("/conversations?q=1", headers=NAVIGATE, follow_redirects=False)
        assert resp.status_code == 303
        target = urlsplit(resp.headers["location"])
        assert target.path == "/auth/sign-in"
        assert parse_qs(target.query)["next"] == ["/conversations?q=1"]

    def test_fetch_gets_401_asking_for_sign_in(self, client: TestClient) -> None:
        resp = client.get("/conversations/api/list", headers=FETCH)
        assert resp.status_code == 401
        assert resp.headers[SESSION_HEADER] == "sign-in"

    def test_a_forged_access_cookie_is_refused(self, client: TestClient) -> None:
        client.cookies.set(
            ACCESS_COOKIE, "eyJhbGciOiJFZERTQSJ9.e30.AAAA", domain="spark.example.com"
        )
        resp = client.get("/auth/session", headers=FETCH)
        assert resp.status_code == 401
        assert resp.headers[SESSION_HEADER] == "sign-in"

    def test_endpoints_never_fall_back_to_the_process_guid(self, client: TestClient) -> None:
        _conversations(client)
        resp = client.get("/conversations/api/list", headers=FETCH)
        assert resp.status_code == 401

    def test_the_container_health_check_needs_no_session(self, client: TestClient) -> None:
        resp = client.get("/loading/api/status", headers={"Host": "127.0.0.1:8000"})
        assert resp.status_code == 200
        assert "stage" in resp.json()

    def test_other_loading_paths_still_need_a_session(self, client: TestClient) -> None:
        assert client.get("/loading", headers=FETCH).status_code == 401


# -- Server-side refresh ------------------------------------------------------------------


class TestRefresh:
    def test_navigation_with_an_expired_token_is_refreshed_and_stays_signed_in(
        self, ada: TestClient
    ) -> None:
        old_refresh = ada.cookies.get(REFRESH_COOKIE)
        _replace_access(ada, _expired_access_token(ada))
        resp = ada.get("/auth/session?probe=1", headers=NAVIGATE, follow_redirects=False)
        assert resp.status_code == 303
        target = urlsplit(resp.headers["location"])
        assert target.path == REFRESH_PATH
        assert parse_qs(target.query)["next"] == ["/auth/session?probe=1"]

        rotated = ada.get(resp.headers["location"], headers=NAVIGATE, follow_redirects=False)
        assert rotated.status_code == 303
        assert rotated.headers["location"] == "/auth/session?probe=1"
        assert attributes(cookie_header(rotated, REFRESH_COOKIE))["path"] == REFRESH_PATH
        cookie_header(rotated, ACCESS_COOKIE)
        assert ada.cookies.get(REFRESH_COOKIE) != old_refresh

        back = ada.get("/auth/session", headers=FETCH)
        assert back.status_code == 200
        assert back.json()["email"] == ADA

    def test_fetch_with_an_expired_token_gets_401_asking_for_refresh(self, ada: TestClient) -> None:
        _replace_access(ada, _expired_access_token(ada))
        resp = ada.get("/auth/session", headers=FETCH)
        assert resp.status_code == 401
        assert resp.headers[SESSION_HEADER] == "refresh"
        refreshed = ada.post(REFRESH_PATH)
        assert refreshed.status_code == 204
        cookie_header(refreshed, ACCESS_COOKIE)
        cookie_header(refreshed, REFRESH_COOKIE)
        assert ada.get("/auth/session", headers=FETCH).status_code == 200

    def test_a_stream_with_an_expired_token_gets_401_asking_for_refresh(
        self, ada: TestClient
    ) -> None:
        _replace_access(ada, _expired_access_token(ada))
        resp = ada.get(
            "/stream/chat?conversation_id=1&message=x", headers={"Accept": "text/event-stream"}
        )
        assert resp.status_code == 401
        assert resp.headers[SESSION_HEADER] == "refresh"

    def test_refresh_post_needs_the_csrf_token(self, ada: TestClient) -> None:
        _replace_access(ada, _expired_access_token(ada))
        del ada.headers[CSRF_HEADER]
        resp = ada.post(REFRESH_PATH)
        assert resp.status_code == 403
        assert resp.json()["detail"] == CSRF_REFUSED

    def test_refresh_post_needs_the_public_origin(self, ada: TestClient) -> None:
        resp = ada.post(REFRESH_PATH, headers={"Origin": "https://example.org"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == ORIGIN_REFUSED

    def test_cross_site_navigation_to_refresh_is_refused(self, ada: TestClient) -> None:
        resp = ada.get(
            REFRESH_PATH,
            headers={**NAVIGATE, "Sec-Fetch-Site": "cross-site"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert not set_cookie_headers(resp)

    def test_a_refused_refresh_signs_out(self, ada: TestClient) -> None:
        ada.cookies.delete(REFRESH_COOKIE)
        ada.cookies.set(REFRESH_COOKIE, "x" * 43, domain="spark.example.com", path=REFRESH_PATH)
        resp = ada.post(REFRESH_PATH)
        assert resp.status_code == 401
        assert resp.headers[SESSION_HEADER] == "sign-in"
        assert attributes(cookie_header(resp, ACCESS_COOKIE))["max-age"] == "0"

    def test_refresh_cookie_is_never_read_outside_the_refresh_path(self, ada: TestClient) -> None:
        # Even a refresh cookie the browser would send everywhere is ignored off the path.
        refresh = ada.cookies.get(REFRESH_COOKIE) or ""
        _replace_access(ada, _expired_access_token(ada))
        ada.cookies.set(REFRESH_COOKIE, refresh, domain="spark.example.com", path="/")
        for headers in (FETCH, NAVIGATE):
            resp = ada.get("/auth/session", headers=headers, follow_redirects=False)
            assert resp.status_code in (303, 401), resp.status_code
            assert not set_cookie_headers(resp)
        resp = ada.post("/conversations/api/create", json={"name": "x"})
        assert resp.status_code == 401
        assert not set_cookie_headers(resp)

    def test_session_csrf_token_survives_a_refresh(self, ada: TestClient) -> None:
        before = ada.headers[CSRF_HEADER]
        _replace_access(ada, _expired_access_token(ada))
        assert ada.post(REFRESH_PATH).status_code == 204
        assert ada.get("/auth/session", headers=FETCH).json()["csrf_token"] == before


# -- CSRF and Origin ---------------------------------------------------------------------


def _unsafe_routes(client: TestClient) -> list[tuple[str, str]]:
    """Every state-changing route the app serves (read from the endpoint routers)."""
    from tests.test_csrf_routes import STATE_CHANGING, _concrete

    return [(method, _concrete(path)) for _, method, path in STATE_CHANGING]


class TestCsrf:
    def test_every_state_changing_route_refuses_a_missing_token(self, ada: TestClient) -> None:
        del ada.headers[CSRF_HEADER]
        routes = _unsafe_routes(ada)
        assert len(routes) > 50
        failures = []
        for method, path in routes:
            resp = ada.request(method, path, follow_redirects=False)
            if resp.status_code != 403 or resp.json().get("detail") != CSRF_REFUSED:
                failures.append((method, path, resp.status_code))
        assert failures == []

    def test_a_token_from_another_session_is_refused(self, ada: TestClient) -> None:
        make_user(ada, GRACE)
        grace = another_client(ada)
        sign_in(grace, GRACE)
        theirs = grace.headers[CSRF_HEADER]
        assert theirs and theirs != ada.headers[CSRF_HEADER]
        ada.headers[CSRF_HEADER] = theirs
        failures = []
        for method, path in _unsafe_routes(ada):
            resp = ada.request(method, path, follow_redirects=False)
            if resp.status_code != 403:
                failures.append((method, path, resp.status_code))
        assert failures == []

    def test_a_second_session_of_the_same_user_has_its_own_token(self, ada: TestClient) -> None:
        second = another_client(ada)
        sign_in(second, ADA)
        assert second.headers[CSRF_HEADER] != ada.headers[CSRF_HEADER]
        ada.headers[CSRF_HEADER] = second.headers[CSRF_HEADER]
        assert ada.post("/api/heartbeat").status_code == 403

    def test_the_right_token_is_accepted(self, ada: TestClient) -> None:
        assert ada.post("/api/heartbeat").status_code == 200


class TestOrigin:
    def test_the_policy_is_pinned_to_the_public_origin(self, client: TestClient) -> None:
        policy = client.app.state.origin_policy  # type: ignore[attr-defined]
        assert policy == OriginPolicy(frozenset({PUBLIC}))
        assert not policy.match_host

    def test_a_post_from_another_site_is_refused(self, ada: TestClient) -> None:
        resp = ada.post("/api/heartbeat", headers={"Origin": "https://example.org"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == ORIGIN_REFUSED

    @pytest.mark.parametrize("path", ["/stream/chat", "/stream/debate", "/stream/panel"])
    def test_a_stream_from_another_site_is_refused(self, ada: TestClient, path: str) -> None:
        resp = ada.get(
            f"{path}?conversation_id=1&message=x", headers={"Origin": "https://example.org"}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == ORIGIN_REFUSED

    def test_a_stream_without_provenance_is_refused(self, ada: TestClient) -> None:
        del ada.headers["Origin"]
        resp = ada.get("/stream/chat?conversation_id=1&message=x")
        assert resp.status_code == 403

    def test_a_stream_from_the_public_origin_is_accepted(self, ada: TestClient) -> None:
        resp = ada.get("/stream/chat?conversation_id=1&message=x")
        assert resp.status_code == 200

    def test_host_spoofing_does_not_widen_the_pin(self, ada: TestClient) -> None:
        spoofed = {"Host": "evil.example.net", "Origin": "https://evil.example.net"}
        assert ada.post("/api/heartbeat", headers=spoofed).status_code == 403
        assert (
            ada.get("/stream/chat?conversation_id=1&message=x", headers=spoofed).status_code == 403
        )
        # The right Origin under another Host is refused too.
        assert ada.post("/api/heartbeat", headers={"Host": "evil.example.net"}).status_code == 403


# -- API keys -----------------------------------------------------------------------------


def _api_key(client: TestClient, user_id: uuid.UUID, scopes: set[str]) -> tuple[str, uuid.UUID]:
    keys = runtime(client).identity.api_keys

    async def create() -> tuple[str, uuid.UUID]:
        new = await keys.create(user_id, "test key", scopes, actor="test")
        return new.key, new.api_key.id

    created: tuple[str, uuid.UUID] = client.portal.call(create)  # type: ignore[union-attr]
    return created


@pytest.fixture
def keyed(client: TestClient) -> tuple[TestClient, str, uuid.UUID]:
    user_id = make_user(client, ADA, ADMINISTRATOR)
    key, key_id = _api_key(client, user_id, {API_KEYS})
    return client, key, key_id


class TestApiKeys:
    def test_a_key_authenticates_limited_to_its_scopes(self, keyed: Any) -> None:
        client, key, _ = keyed
        resp = client.get("/auth/session", headers={"Authorization": f"Bearer {key}", **FETCH})
        assert resp.status_code == 200
        assert resp.json()["permissions"] == [API_KEYS]
        assert resp.json()["via"] == "api_key"

    def test_a_key_needs_no_csrf_token_and_no_origin(self, keyed: Any) -> None:
        client, key, _ = keyed
        resp = client.post("/api/heartbeat", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200

    def test_a_browser_request_with_a_key_is_still_origin_checked(self, keyed: Any) -> None:
        client, key, _ = keyed
        headers = {"Authorization": f"Bearer {key}", "Origin": "https://example.org"}
        assert client.post("/api/heartbeat", headers=headers).status_code == 403

    def test_a_revoked_key_is_refused(self, keyed: Any) -> None:
        client, key, key_id = keyed
        keys = runtime(client).identity.api_keys
        client.portal.call(lambda: keys.revoke(key_id, actor="test"))
        resp = client.get("/auth/session", headers={"Authorization": f"Bearer {key}", **FETCH})
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "token", ["wdn_" + "A" * 43, "wdn_not-a-real-key", "eyJ.not.a-token", "wdn_"]
    )
    def test_a_wrong_key_is_refused(self, keyed: Any, token: str) -> None:
        client, _, _ = keyed
        resp = client.get("/auth/session", headers={"Authorization": f"Bearer {token}", **FETCH})
        assert resp.status_code == 401
        resp = client.post("/api/heartbeat", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_a_key_and_a_session_cookie_together_are_refused(self, keyed: Any) -> None:
        client, key, _ = keyed
        sign_in(client, ADA)
        del client.headers[CSRF_HEADER]
        resp = client.post("/api/heartbeat", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 401


# -- Sign-out ------------------------------------------------------------------------------


class TestSignOut:
    def test_sign_out_revokes_the_session_and_clears_both_cookies(self, ada: TestClient) -> None:
        access = ada.cookies.get(ACCESS_COOKIE) or ""
        refresh = ada.cookies.get(REFRESH_COOKIE) or ""
        resp = ada.post("/auth/sign-out", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/sign-in"
        cleared_access = attributes(cookie_header(resp, ACCESS_COOKIE))
        cleared_refresh = attributes(cookie_header(resp, REFRESH_COOKIE))
        assert cleared_access["max-age"] == "0" and cleared_access["path"] == "/"
        assert cleared_refresh["max-age"] == "0" and cleared_refresh["path"] == REFRESH_PATH
        # The old tokens are dead, not just forgotten.
        _replace_access(ada, access)
        assert ada.get("/auth/session", headers=FETCH).status_code == 401
        ada.cookies.set(REFRESH_COOKIE, refresh, domain="spark.example.com", path=REFRESH_PATH)
        assert ada.post(REFRESH_PATH).status_code in (401, 403)

    def test_sign_out_needs_the_csrf_token(self, ada: TestClient) -> None:
        del ada.headers[CSRF_HEADER]
        assert ada.post("/auth/sign-out").status_code == 403
        assert ada.get("/auth/session", headers=FETCH).status_code == 200

    def test_the_menu_form_signs_out_through_warden(self, ada: TestClient) -> None:
        resp = ada.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/sign-in"
        assert ada.get("/auth/session", headers=FETCH).status_code == 401


# -- Per-user data -------------------------------------------------------------------------


class TestPerUser:
    def test_two_users_see_only_their_own_conversations(self, ada: TestClient) -> None:
        _conversations(ada)
        make_user(ada, GRACE)
        grace = another_client(ada)
        sign_in(grace, GRACE)

        made = ada.post("/conversations/api/create", json={"name": "Ada's notes", "model_id": "m"})
        assert made.status_code == 200, made.text
        grace.post("/conversations/api/create", json={"name": "Grace's notes", "model_id": "m"})

        ada_names = [c["name"] for c in ada.get("/conversations/api/list").json()]
        grace_names = [c["name"] for c in grace.get("/conversations/api/list").json()]
        assert ada_names == ["Ada's notes"]
        assert grace_names == ["Grace's notes"]

    def test_the_request_guid_is_the_mapped_warden_user(self, ada: TestClient) -> None:
        body = ada.get("/auth/session", headers=FETCH).json()
        process_guid = ada.app.state.user_guid  # type: ignore[attr-defined]
        assert body["user_guid"] != process_guid
        assert body["user_guid"] == body["warden_user_id"]
