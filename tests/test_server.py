"""Tests for the FastAPI web server."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.web.server import create_app


@pytest.fixture()
def mock_ctx() -> MagicMock:
    """Create a mock AppContext with minimal settings."""
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    return ctx


def _mock_settings_get(
    key: str, default: object = None, *, cast: type | None = None
) -> object:
    """Mock settings.get that returns sensible defaults."""
    values = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
        "interface.ssl.enabled": False,
        "providers.anthropic.enabled": False,
        "providers.aws_bedrock.enabled": False,
        "providers.ollama.enabled": False,
        "providers.google_gemini.enabled": False,
        "providers.xai.enabled": False,
    }
    val = values.get(key, default)
    if cast is not None and val is not None:
        val = cast(val)
    return val


@pytest.fixture()
def app(mock_ctx: MagicMock) -> TestClient:
    """Create a test client for the Spark app."""
    application = create_app(mock_ctx, first_run=False)
    return TestClient(application)


@pytest.fixture()
def first_run_app(mock_ctx: MagicMock) -> TestClient:
    """Create a test client with first_run=True."""
    application = create_app(mock_ctx, first_run=True)
    return TestClient(application)


class TestAuthMiddleware:
    def test_login_page_is_public(self, app: TestClient) -> None:
        resp = app.get("/login", follow_redirects=False)
        assert resp.status_code == 200

    def test_unauthenticated_redirects_to_login(self, app: TestClient) -> None:
        resp = app.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_static_is_public(self, app: TestClient) -> None:
        resp = app.get("/static/css/spark.css", follow_redirects=False)
        assert resp.status_code == 200

    def test_invalid_session_redirects(self, app: TestClient) -> None:
        resp = app.get(
            "/", cookies={"spark_session": "invalid"}, follow_redirects=False
        )
        assert resp.status_code == 303


class TestAuthFlow:
    def test_login_with_valid_code(self, app: TestClient) -> None:
        # Get the auth code from the app state
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        resp = app.post("/api/auth", data={"code": code}, follow_redirects=False)
        assert resp.status_code == 303
        assert "spark_session" in resp.cookies

    def test_login_with_invalid_code(self, app: TestClient) -> None:
        resp = app.post("/api/auth", data={"code": "BADCODE1"}, follow_redirects=False)
        assert resp.status_code == 401

    def test_authenticated_access(self, app: TestClient) -> None:
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = app.post("/api/auth", data={"code": code}, follow_redirects=False)
        session_cookie = login_resp.cookies.get("spark_session")
        assert session_cookie

        resp = app.get(
            "/", cookies={"spark_session": session_cookie}, follow_redirects=False
        )
        assert resp.status_code == 200

    def test_logout(self, app: TestClient) -> None:
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = app.post("/api/auth", data={"code": code}, follow_redirects=False)
        session_cookie = login_resp.cookies.get("spark_session")

        resp = app.get(
            "/logout", cookies={"spark_session": session_cookie}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestAutoLogin:
    def test_auto_login_valid_code(self, app: TestClient) -> None:
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        resp = app.get(f"/auto-login?code={code}", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/loading"
        assert "spark_session" in resp.cookies

    def test_auto_login_invalid_code(self, app: TestClient) -> None:
        resp = app.get("/auto-login?code=BADCODE1", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_auto_login_is_public(self, app: TestClient) -> None:
        resp = app.get("/auto-login?code=X", follow_redirects=False)
        # Should not redirect to /login via middleware (it IS public)
        assert resp.status_code == 303
        assert (
            resp.headers["location"] == "/login"
        )  # Invalid code → redirect to login page


class TestFirstRun:
    def test_root_redirects_to_welcome(self, first_run_app: TestClient) -> None:
        code = first_run_app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = first_run_app.post(
            "/api/auth", data={"code": code}, follow_redirects=False
        )
        session_cookie = login_resp.cookies.get("spark_session")

        resp = first_run_app.get(
            "/", cookies={"spark_session": session_cookie}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/welcome"

    def test_welcome_page_loads(self, first_run_app: TestClient) -> None:
        code = first_run_app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = first_run_app.post(
            "/api/auth", data={"code": code}, follow_redirects=False
        )
        session_cookie = login_resp.cookies.get("spark_session")

        resp = first_run_app.get(
            "/welcome",
            cookies={"spark_session": session_cookie},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Welcome to" in resp.text
        assert "Spark" in resp.text


class TestMainMenu:
    def test_menu_page_loads(self, app: TestClient) -> None:
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = app.post("/api/auth", data={"code": code}, follow_redirects=False)
        session_cookie = login_resp.cookies.get("spark_session")

        resp = app.get(
            "/menu", cookies={"spark_session": session_cookie}, follow_redirects=False
        )
        assert resp.status_code == 200
        assert "Dashboard" in resp.text


class TestSettingsPage:
    def test_settings_page_loads(self, app: TestClient) -> None:
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = app.post("/api/auth", data={"code": code}, follow_redirects=False)
        session_cookie = login_resp.cookies.get("spark_session")

        resp = app.get(
            "/settings",
            cookies={"spark_session": session_cookie},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "Settings" in resp.text
        assert "LLM Providers" in resp.text

    def test_save_settings(
        self, app: TestClient, tmp_path: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code = app.app.state.auth.generate_code()  # type: ignore[union-attr]
        login_resp = app.post("/api/auth", data={"code": code}, follow_redirects=False)
        session_cookie = login_resp.cookies.get("spark_session")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("database:\n  type: sqlite\n")

        # Monkeypatch _get_config_path to return our temp file
        import spark.core.application as app_mod

        monkeypatch.setattr(app_mod, "_get_config_path", lambda: config_file)

        resp = app.post(
            "/settings/api/save",
            cookies={"spark_session": session_cookie},
            json={"database.type": "postgresql"},
        )
        assert resp.status_code == 200

        import yaml

        saved = yaml.safe_load(config_file.read_text())
        assert saved["database"]["type"] == "postgresql"


class TestPortResolution:
    def _ctx(self, values: dict):
        ctx = MagicMock()

        def get(key, default=None, *, cast=None):
            val = values.get(key, default)
            if cast is not None and val is not None:
                val = cast(val)
            return val

        ctx.settings.get = get
        return ctx

    def test_zero_port_uses_a_random_free_port(self) -> None:
        from spark.web.server import _resolve_port

        port = _resolve_port(self._ctx({"interface.port": 0}), "127.0.0.1")
        assert isinstance(port, int) and 1024 <= port <= 65535

    def test_missing_port_uses_a_random_free_port(self) -> None:
        from spark.web.server import _resolve_port

        port = _resolve_port(self._ctx({}), "127.0.0.1")
        assert isinstance(port, int) and port > 0

    def test_fixed_port_is_used_verbatim(self) -> None:
        from spark.web.server import _resolve_port

        assert _resolve_port(self._ctx({"interface.port": 8765}), "127.0.0.1") == 8765


class TestBrowserOpenSetting:
    def _ctx(self, values: dict):
        ctx = MagicMock()

        def get(key, default=None, *, cast=None):
            val = values.get(key, default)
            if cast is not None and val is not None:
                val = cast(val)
            return val

        ctx.settings.get = get
        return ctx

    def test_default_opens_browser(self) -> None:
        from spark.web.server import _should_open_browser

        assert _should_open_browser(self._ctx({})) is True

    def test_disabled_does_not_open_browser(self) -> None:
        from spark.web.server import _should_open_browser

        assert (
            _should_open_browser(self._ctx({"interface.open_browser": False})) is False
        )

    def test_string_false_from_env_var_does_not_open_browser(self) -> None:
        # konfig delivers SPARK__INTERFACE__OPEN_BROWSER=false as the string
        # "false", which bool() would wrongly treat as truthy.
        from spark.web.server import _should_open_browser

        for value in ("false", "False", "0", "no", "off"):
            assert (
                _should_open_browser(self._ctx({"interface.open_browser": value}))
                is False
            )

    def test_string_true_from_env_var_opens_browser(self) -> None:
        from spark.web.server import _should_open_browser

        assert (
            _should_open_browser(self._ctx({"interface.open_browser": "true"})) is True
        )
