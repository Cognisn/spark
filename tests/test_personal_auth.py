"""Personal-mode sign-in, sign-out, cookies and CSRF wiring through the web app."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spark.web.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    AuthManager,
    LoginPolicy,
    PersonalAuthenticator,
    WebAuthenticator,
)
from spark.web.security import CSRF_FORM_FIELD, CSRF_HEADER, OriginPolicy
from spark.web.server import create_app
from tests.web_helpers import ORIGIN, login_token, sign_in

_BASE_SETTINGS: dict[str, Any] = {
    "interface.session_timeout_minutes": 60,
    "interface.host": "127.0.0.1",
    "interface.ssl.enabled": False,
}


def _ctx(**overrides: Any) -> MagicMock:
    values = {**_BASE_SETTINGS, **overrides}

    def get(key: str, default: object = None, *, cast: type | None = None) -> object:
        val = values.get(key, default)
        if cast is not None and val is not None:
            val = cast(val)
        return val

    ctx = MagicMock()
    ctx.settings.get = get
    return ctx


@pytest.fixture()
def app() -> FastAPI:
    return create_app(_ctx(), first_run=False)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _set_cookie_headers(resp: httpx.Response, name: str) -> list[str]:
    return [h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{name}=")]


def _post_code(client: TestClient, code: str, **headers: str) -> httpx.Response:
    return client.post(
        "/api/auth",
        data={"code": code, CSRF_FORM_FIELD: login_token(client)},
        headers={"Origin": ORIGIN, **headers},
        follow_redirects=False,
    )


class TestInterface:
    def test_app_exposes_a_swappable_authenticator(self, app: FastAPI) -> None:
        assert isinstance(app.state.authenticator, PersonalAuthenticator)
        assert isinstance(app.state.authenticator, WebAuthenticator)

    def test_settings_shape_the_login_policy(self) -> None:
        app = create_app(
            _ctx(
                **{
                    "interface.login.code_ttl_seconds": 120,
                    "interface.login.max_failures": 3,
                    "interface.login.failure_window_seconds": 60,
                    "interface.login.lockout_seconds": 600,
                }
            )
        )
        assert app.state.auth.policy == LoginPolicy(120, 3, 60, 600)

    def test_until_served_the_origin_is_the_request_host(self, app: FastAPI) -> None:
        assert app.state.origin_policy == OriginPolicy.same_host()

    def test_a_fixed_port_names_the_origin_up_front(self) -> None:
        app = create_app(_ctx(**{"interface.port": 22400}))
        assert app.state.origin_policy == OriginPolicy.for_instance("http", "127.0.0.1", 22400)


class TestServe:
    async def test_serving_pins_the_origin_to_the_resolved_address(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        import spark.web.server as server
        from spark import mode

        seen: dict[str, Any] = {}

        class FakeServer:
            def __init__(self, config: Any) -> None:
                self.config = config

            async def serve(self) -> None:
                seen["policy"] = self.config.app.state.origin_policy
                seen["port"] = self.config.port

        runtime = MagicMock()
        runtime.mode = mode.Mode.PERSONAL
        monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
        monkeypatch.setattr(server, "_background_init", lambda app, ctx: None)
        monkeypatch.setattr(mode, "current", lambda: runtime)
        monkeypatch.setattr("spark.core.application._get_data_path", lambda: tmp_path)

        ctx = _ctx(**{"interface.open_browser": False})
        await server.create_and_serve(ctx)
        assert seen["policy"] == OriginPolicy.for_instance("http", "127.0.0.1", seen["port"])


class TestLoginPage:
    def test_sets_a_strict_http_only_login_token_cookie(self, client: TestClient) -> None:
        resp = client.get("/login")
        assert resp.status_code == 200
        (cookie,) = _set_cookie_headers(resp, LOGIN_CSRF_COOKIE)
        lowered = cookie.lower()
        assert "httponly" in lowered
        assert "samesite=strict" in lowered
        assert "path=/api/auth" in lowered
        token = client.cookies.get(LOGIN_CSRF_COOKIE)
        assert token and f'name="{CSRF_FORM_FIELD}" value="{token}"' in resp.text

    def test_issues_and_logs_a_code_when_none_is_live(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="spark.web"):
            client.get("/login")
        match = re.search(r"Authentication code: ([0-9A-F]{8})", caplog.text)
        assert match, caplog.text
        assert _post_code(client, match.group(1)).status_code == 303

    def test_issues_nothing_while_a_code_is_live(
        self, app: FastAPI, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        live = app.state.auth.generate_code()
        with caplog.at_level(logging.INFO, logger="spark.web"):
            client.get("/login")
        assert "Authentication code" not in caplog.text
        assert _post_code(client, live).status_code == 303


class TestSignIn:
    def test_valid_code_creates_a_strict_session_cookie(
        self, app: FastAPI, client: TestClient
    ) -> None:
        resp = _post_code(client, app.state.auth.generate_code())
        assert resp.status_code == 303
        assert resp.headers["location"] == "/loading"
        (cookie,) = _set_cookie_headers(resp, SESSION_COOKIE)
        lowered = cookie.lower()
        assert "httponly" in lowered
        assert "samesite=strict" in lowered
        assert "secure" not in lowered

    def test_session_cookie_is_secure_over_tls(self, app: FastAPI) -> None:
        tls = TestClient(app, base_url="https://testserver")
        tls.get("/login")
        resp = tls.post(
            "/api/auth",
            data={
                "code": app.state.auth.generate_code(),
                CSRF_FORM_FIELD: tls.cookies.get(LOGIN_CSRF_COOKIE),
            },
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        (cookie,) = _set_cookie_headers(resp, SESSION_COOKIE)
        assert "secure" in cookie.lower()

    def test_sign_in_needs_the_login_token(self, app: FastAPI, client: TestClient) -> None:
        client.get("/login")
        resp = client.post(
            "/api/auth",
            data={"code": app.state.auth.generate_code()},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert SESSION_COOKIE not in resp.cookies

    def test_sign_in_from_another_origin_is_refused(self, app: FastAPI, client: TestClient) -> None:
        resp = _post_code(client, app.state.auth.generate_code(), Origin="http://example.com")
        assert resp.status_code == 403

    def test_code_is_single_use(self, app: FastAPI, client: TestClient) -> None:
        code = app.state.auth.generate_code()
        assert _post_code(client, code).status_code == 303
        resp = _post_code(client, code)
        assert resp.status_code == 401
        assert SESSION_COOKIE not in resp.cookies

    def test_expired_code_is_refused(self, app: FastAPI, client: TestClient) -> None:
        now = [100.0]
        app.state.auth = AuthManager(LoginPolicy(code_ttl_seconds=60), clock=lambda: now[0])
        code = app.state.auth.generate_code()
        now[0] += 61
        assert _post_code(client, code).status_code == 401

    def test_lock_out_after_repeated_failures(self, app: FastAPI, client: TestClient) -> None:
        app.state.auth = AuthManager(LoginPolicy(max_failures=3))
        code = app.state.auth.generate_code()
        for _ in range(3):
            assert _post_code(client, "00000000").status_code == 401
        resp = _post_code(client, code)
        assert resp.status_code == 429
        assert "Too many failed" in resp.text
        assert SESSION_COOKIE not in resp.cookies

    def test_failures_are_logged_without_the_code(
        self, app: FastAPI, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        app.state.auth.generate_code()
        with caplog.at_level(logging.DEBUG):
            _post_code(client, "C0FFEE77")
        assert "Sign-in failed" in caplog.text
        assert "C0FFEE77" not in caplog.text.upper()

    async def test_parallel_redemption_of_one_code_signs_in_once(self, app: FastAPI) -> None:
        # Every attempt comes from one address; lift the lock-out to isolate single use.
        app.state.auth = AuthManager(LoginPolicy(max_failures=1000))
        code = app.state.auth.generate_code()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as setup:
            await setup.get("/login")
            token = setup.cookies.get(LOGIN_CSRF_COOKIE)

        async def attempt() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=transport, base_url=ORIGIN, cookies={LOGIN_CSRF_COOKIE: token or ""}
            ) as c:
                return await c.post(
                    "/api/auth",
                    data={"code": code, CSRF_FORM_FIELD: token or ""},
                    headers={"Origin": ORIGIN},
                    follow_redirects=False,
                )

        responses = await asyncio.gather(*(attempt() for _ in range(20)))
        statuses = sorted(r.status_code for r in responses)
        assert statuses.count(303) == 1
        assert statuses.count(401) == 19


class TestAutoLogin:
    def test_first_use_signs_in(self, app: FastAPI, client: TestClient) -> None:
        resp = client.get(
            f"/auto-login?code={app.state.auth.generate_code()}", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/loading"
        (cookie,) = _set_cookie_headers(resp, SESSION_COOKIE)
        assert "samesite=strict" in cookie.lower()

    def test_code_cannot_be_reused(self, app: FastAPI, client: TestClient) -> None:
        code = app.state.auth.generate_code()
        client.get(f"/auto-login?code={code}", follow_redirects=False)
        client.cookies.clear()
        again = client.get(f"/auto-login?code={code}", follow_redirects=False)
        assert again.status_code == 303
        assert again.headers["location"] == "/login"
        assert SESSION_COOKIE not in again.cookies
        assert _post_code(client, code).status_code == 401


class TestSignOut:
    def test_sign_out_ends_the_session(self, app: FastAPI, client: TestClient) -> None:
        cookies = sign_in(client)
        resp = client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        assert app.state.session.validate(cookies[SESSION_COOKIE]) is False
        cleared = _set_cookie_headers(resp, SESSION_COOKIE)
        assert cleared and "max-age=0" in cleared[0].lower()

    def test_sign_out_needs_the_csrf_token(self, app: FastAPI, client: TestClient) -> None:
        cookies = sign_in(client)
        del client.headers[CSRF_HEADER]
        assert client.post("/logout", follow_redirects=False).status_code == 403
        assert app.state.session.validate(cookies[SESSION_COOKIE]) is True

    def test_sign_out_by_get_is_not_allowed(self, app: FastAPI, client: TestClient) -> None:
        cookies = sign_in(client)
        assert client.get("/logout", follow_redirects=False).status_code == 405
        assert app.state.session.validate(cookies[SESSION_COOKIE]) is True

    def test_old_cookie_is_refused_after_sign_out(self, client: TestClient) -> None:
        sign_in(client)
        client.post("/logout", follow_redirects=False)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestPublicPaths:
    @pytest.mark.parametrize("path", ["/loginx", "/api/authx", "/staticx", "/loadingx"])
    def test_lookalike_paths_are_not_public(self, client: TestClient, path: str) -> None:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestPages:
    def test_signed_in_pages_carry_the_session_token(
        self, app: FastAPI, client: TestClient
    ) -> None:
        cookies = sign_in(client)
        token = app.state.session.csrf_token(cookies[SESSION_COOKIE])
        page = client.get("/settings")
        assert page.status_code == 200
        assert f'<meta name="csrf-token" content="{token}">' in page.text
        assert '<script src="/static/js/csrf.js"></script>' in page.text

    def test_loading_page_carries_the_session_token(self, app: FastAPI, client: TestClient) -> None:
        cookies = sign_in(client)
        token = app.state.session.csrf_token(cookies[SESSION_COOKIE])
        page = client.get("/loading")
        assert f'<meta name="csrf-token" content="{token}">' in page.text

    def test_loading_page_has_no_token_before_sign_in(self, client: TestClient) -> None:
        page = client.get("/loading")
        assert '<meta name="csrf-token" content="">' in page.text
