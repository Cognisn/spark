"""The Cognisn sign-in and app chrome standard (CS1 to CS11, CS19, CS20) for SPARK.

Guards under test:

* every page before sign-in renders on one frame (mark, name, descriptor, one h1, labelled
  fields, the Cognisn endorsement below the card) and shows no version, no environment name and
  no third-party font request;
* the theme is set before any stylesheet paints, from the saved choice or the device;
* the sign-in page offers the passkey and single sign-on buttons only when they are offered,
  under one "or" divider, with "Forgotten your password?" last;
* the header carries the bolt and SPARK (no Cognisn logo, no version); the footer carries the
  version, the endorsement and the copyright line with the current year;
* the Theme row (Device, Light, Dark) is in the Account menu and the header toggle is gone;
* Quit and the folder items are shown in personal mode only;
* the theme preference accepts device, light and dark and refuses anything else;
* spark-bolt.svg is the bolt polygon of the loading page, unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import spark
from spark import mode
from spark.instance_settings import registry
from spark.instance_settings.registry import InvalidSettingError
from spark.mode import Mode, RuntimeSettings
from spark.web.endpoints.identity import SIGN_IN_FAILED
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_instance_settings_web import Ctx, MemorySecrets
from tests.web_helpers import sign_in as personal_sign_in

WEB = Path(spark.__file__).resolve().parent / "web"
TEMPLATES = WEB / "templates"
STATIC = WEB / "static"
YEAR = str(datetime.now().year)
POLYGON = re.compile(r'<polygon\s+points="([^"]+)"')


def _env() -> Any:
    env = Jinja2Templates(directory=str(TEMPLATES)).env
    env.globals["version"] = "9.8.7-test"
    env.globals["current_year"] = lambda: datetime.now().year
    env.globals["csrf_token"] = lambda _request=None: "csrf-test"
    env.globals["identity_menu"] = lambda _request: []
    env.globals["admin_menu"] = lambda _request: []
    env.globals["actions_enabled"] = lambda: False
    return env


def render(name: str, **context: Any) -> str:
    if name not in ("base.html", "loading.html"):  # these call the csrf_token global
        context.setdefault("csrf_token", "csrf-test")
    context.setdefault("next_path", "/")
    context.setdefault("request", _request())
    return str(_env().get_template(name).render(**context))


def _request(server: bool = False) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(identity_attached=server)),
        url=SimpleNamespace(path="/"),
    )


# Every template a person can see before signing in, with a context that shows its main form.
PRE_SIGN_IN: dict[str, dict[str, Any]] = {
    "auth_sign_in.html": {},
    "auth_challenge.html": {"purpose": "verify", "totp": True, "webauthn": False},
    "auth_invitation.html": {"token": "tok", "min_length": 12},
    "auth_reset.html": {"token": "tok", "min_length": 12},
    "auth_reset_request.html": {},
    "auth_recovery_codes.html": {"recovery_codes": ["aaaaa-bbbbb"], "next_path": "/"},
    "auth_sso.html": {"signed_in": False},
    "auth_passkey_register.html": {
        "token": "tok",
        "kind": "link",
        "action": "/x",
        "options_url": "/y",
    },
    "login.html": {},
    "mcp_sign_in.html": {"signed_in": True, "server": "example-server"},
}


@pytest.fixture(params=sorted(PRE_SIGN_IN))
def page(request: pytest.FixtureRequest) -> tuple[str, str]:
    name: str = request.param
    return name, render(name, **PRE_SIGN_IN[name])


class TestFrame:
    def test_every_page_renders_the_frame(self, page: tuple[str, str]) -> None:
        name, html = page
        assert 'class="auth-card"' in html, name
        assert '<img class="brand-mark ' in html and "/static/img/spark-bolt.svg" in html, name
        assert '<p class="auth-name">SPARK</p>' in html, name
        assert '<p class="auth-descriptor">Secure Personal AI Research Kit</p>' in html, name
        assert html.count("<h1") == 1, name
        assert "/static/css/chrome.css" in html, name

    def test_the_mark_is_in_a_72_px_slot(self, page: tuple[str, str]) -> None:
        _, html = page
        assert 'width="72" height="72"' in html

    def test_cognisn_endorses_below_the_card_with_the_current_year(
        self, page: tuple[str, str]
    ) -> None:
        name, html = page
        card_end = html.index("</div>\n        {#") if False else html.index('class="auth-endorse"')
        assert html.index("auth-card") < card_end, name
        assert "A product of" in html, name
        assert html.count('alt="Cognisn"') == 2, name  # the light and dark logos, one shown
        assert f"&copy; {YEAR} Cognisn Pty Ltd" in html, name
        # Cognisn never leads: nothing before the endorsement mentions it.
        assert "Cognisn" not in html[:card_end].replace("/static/img/cognisn", ""), name

    def test_no_version_build_or_environment_name(self, page: tuple[str, str]) -> None:
        name, html = page
        assert "9.8.7-test" not in html, name
        assert spark.__version__ not in html, name
        assert not re.search(r"\bv\d+\.\d+", html), name
        for word in ("rc", "build", "production", "staging", "environment"):
            assert not re.search(rf">[^<]*\b{word}\b[^<]*<", html, re.I), (name, word)

    def test_no_third_party_font_request(self, page: tuple[str, str]) -> None:
        name, html = page
        assert "fonts.googleapis.com" not in html, name
        assert "fonts.gstatic.com" not in html, name

    def test_theme_is_set_before_any_stylesheet_paints(self, page: tuple[str, str]) -> None:
        name, html = page
        boot = html.index("data-bs-theme")
        assert boot < html.index('rel="stylesheet"'), name
        assert "prefers-color-scheme: light" in html, name
        assert "localStorage.getItem('app-theme')" in html, name

    def test_every_visible_field_has_a_label(self, page: tuple[str, str]) -> None:
        name, html = page
        for field in re.findall(r"<input\b[^>]*>", html):
            if 'type="hidden"' in field:
                continue
            ident = re.search(r'\bid="([^"]+)"', field)
            assert ident, (name, field)
            label = re.search(rf'<label[^>]*\bfor="{re.escape(ident.group(1))}"[^>]*>', html)
            assert label, (name, ident.group(1))
            assert "visually-hidden" not in label.group(0), (name, ident.group(1))


class TestFields:
    def test_sign_in_autocomplete(self) -> None:
        html = render("auth_sign_in.html", passkey_sign_in=False, sso_enabled=False)
        assert 'autocomplete="username"' in html
        assert 'autocomplete="current-password"' in html
        assert "webauthn" not in html.split("<form")[1].split("</form>")[0]

    def test_sign_in_autocomplete_names_webauthn_when_passkeys_are_offered(self) -> None:
        html = render("auth_sign_in.html", passkey_sign_in=True, sso_enabled=False)
        assert 'autocomplete="username webauthn"' in html

    def test_new_passwords_and_codes(self) -> None:
        reset = render("auth_reset.html", **PRE_SIGN_IN["auth_reset.html"])
        assert reset.count('autocomplete="new-password"') == 2
        code = render("auth_challenge.html", **PRE_SIGN_IN["auth_challenge.html"])
        assert 'autocomplete="one-time-code"' in code
        assert 'inputmode="numeric"' in code

    def test_headings_say_what_the_page_is(self) -> None:
        headings = {
            "auth_sign_in.html": "Sign in",
            "auth_reset.html": "Choose a new password",
            "auth_challenge.html": "Check your sign-in",
        }
        for name, heading in headings.items():
            html = render(name, **PRE_SIGN_IN[name])
            assert re.search(rf'<h1 class="auth-heading">{heading}</h1>', html), name

    def test_the_failure_message_is_one_generic_sentence(self) -> None:
        assert SIGN_IN_FAILED == "The email address or password is incorrect."
        html = render("auth_sign_in.html", error=SIGN_IN_FAILED)
        assert 'role="alert">' in html and SIGN_IN_FAILED in html


class TestOtherMethods:
    def test_none_offered_shows_no_divider_and_no_buttons(self) -> None:
        html = render("auth_sign_in.html", passkey_sign_in=False, sso_enabled=False)
        assert "auth-divider" not in html
        assert "passkey" not in html.lower()
        assert "single sign-on" not in html.lower()

    def test_passkey_then_sso_under_one_divider_then_forgotten_password_last(self) -> None:
        html = render("auth_sign_in.html", passkey_sign_in=True, sso_enabled=True)
        assert html.count('class="auth-divider"') == 1
        divider = html.index("auth-divider")
        passkey = html.index("Sign in with a passkey")
        sso = html.index("Sign in with single sign-on")
        forgotten = html.index("Forgotten your password?")
        assert html.index("Sign in</button>") < divider < passkey < sso < forgotten
        assert 'data-webauthn="sign-in"' in html  # the include point still works
        assert "/static/js/webauthn.js" in html
        assert html.count("Forgotten your password?") == 1
        card = html[: html.index('class="auth-endorse"')]
        assert card.rindex("<a ") == card.index('<a href="/auth/password-reset">')  # the last link

    def test_sso_alone(self) -> None:
        html = render("auth_sign_in.html", passkey_sign_in=False, sso_enabled=True)
        assert html.count("auth-divider") == 1
        assert "Sign in with single sign-on" in html and "passkey" not in html.lower()


class TestChrome:
    def test_header_is_the_bolt_and_the_name_only(self) -> None:
        html = render("base.html")
        nav = html[html.index("<nav") : html.index("</nav>")]
        brand = nav[nav.index("navbar-brand") : nav.index("</a>", nav.index("navbar-brand"))]
        assert "/static/img/spark-bolt.svg" in brand and 'width="28" height="28"' in brand
        assert '<span class="brand-product">SPARK</span>' in brand
        assert "Cognisn" not in brand and "9.8.7-test" not in brand
        assert 'href="/"' in brand

    def test_footer_carries_version_endorsement_and_copyright(self) -> None:
        html = render("base.html")
        footer = html[html.index("<footer") : html.index("</footer>")]
        assert "SPARK v9.8.7-test" in footer
        assert "A product of" in footer and 'alt="Cognisn"' in footer
        assert f"&copy; {YEAR} Cognisn Pty Ltd" in footer
        assert "Secure Personal AI Research Kit" not in footer
        order = [footer.index(t) for t in ("SPARK v", "A product of", "&copy;")]
        assert order == sorted(order)

    def test_the_version_stays_in_help_and_about(self) -> None:
        html = render("base.html")
        assert "Spark v9.8.7-test" in html[html.index("Help") :]

    @pytest.mark.parametrize("server", [False, True])
    def test_the_theme_row_replaces_the_header_toggle(self, server: bool) -> None:
        html = render("base.html", request=_request(server))
        assert "app-theme-toggle" not in html and "theme-option" not in html
        group = html[html.index('role="radiogroup"') :]
        group = group[: group.index("</div>")]
        assert 'aria-labelledby="app-theme-label"' in html
        assert re.search(r'id="app-theme-label">Theme<', html)
        values = re.findall(
            r'<input type="radio" name="app-theme-choice" id="[^"]+" value="(\w+)"', group
        )
        assert values == ["device", "light", "dark"]
        for label in ("Device", "Light", "Dark"):
            assert f">{label}</label>" in group

    def test_personal_mode_keeps_quit_and_the_folder_items(self) -> None:
        html = render("base.html", request=_request(server=False))
        assert "showQuitModal(event)" in html and 'id="quitModal"' in html
        assert "openLogFolder()" in html.split("</nav>")[0]
        assert "openDataFolder()" in html.split("</nav>")[0]

    def test_server_mode_hides_quit_and_the_folder_items(self) -> None:
        html = render("base.html", request=_request(server=True))
        assert "Quit Spark" not in html.split("</nav>")[0]
        assert "showQuitModal(event)" not in html.split("</nav>")[0]
        assert 'id="quitModal"' not in html
        assert "Open Log Folder" not in html and "Open Data Folder" not in html
        assert "openLogFolder()" not in html.split("</nav>")[0]


class TestTheme:
    def test_the_preference_accepts_device_light_and_dark(self) -> None:
        for value in ("device", "light", "dark"):
            assert registry.coerce("interface.theme", value) == value
        assert registry.spec("interface.theme").default == "device"

    @pytest.mark.parametrize("value", ["neon", "", "Dark ", "auto", None])
    def test_the_preference_refuses_anything_else(self, value: Any) -> None:
        with pytest.raises(InvalidSettingError):
            registry.coerce("interface.theme", value)

    def test_the_script_follows_the_device_live(self) -> None:
        script = (STATIC / "js" / "ui.js").read_text(encoding="utf-8")
        assert "DEVICE: 'device'" in script
        assert "addEventListener('change'" in script
        assert 'input[name="app-theme-choice"]' in script


@pytest.fixture
def personal(
    tmp_path: Path, sqlite_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    import spark.core.application as app_mod

    config = tmp_path / "config.yaml"
    # The theme is saved to the config file: never the machine's own.
    monkeypatch.setattr(app_mod, "_get_config_path", lambda: config)
    config.write_text("{}\n", encoding="utf-8", newline="\n")
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    try:
        app = create_app(Ctx(config, MemorySecrets()))  # type: ignore[arg-type]
        with app_client(app, sqlite_settings) as client:
            personal_sign_in(client)
            yield client
    finally:
        mode.reset()


class TestPersonalApp:
    def test_the_real_page_has_the_chrome_and_desktop_controls(self, personal: TestClient) -> None:
        text = personal.get("/").text
        assert f"&copy; {YEAR} Cognisn Pty Ltd" in text
        assert f"SPARK v{spark.__version__}" in text
        assert "app-theme-toggle" not in text
        assert 'name="app-theme-choice"' in text  # the Account menu is there in personal mode too
        assert "Quit Spark" in text and "Open Log Folder" in text

    def test_the_theme_default_is_device_and_the_choice_is_saved(
        self, personal: TestClient
    ) -> None:
        assert personal.get("/api/theme").json() == {"theme": "device"}
        for value in ("light", "dark", "device"):
            assert personal.post("/api/theme", json={"theme": value}).status_code == 200
            assert personal.get("/api/theme").json() == {"theme": value}

    @pytest.mark.parametrize("value", ["neon", "", "auto", 7])
    def test_an_unknown_theme_is_refused(self, personal: TestClient, value: Any) -> None:
        assert personal.post("/api/theme", json={"theme": value}).status_code == 400
        assert personal.get("/api/theme").json() == {"theme": "device"}

    def test_the_login_page_is_on_the_frame(self, tmp_path: Path, sqlite_settings: Any) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("{}\n", encoding="utf-8", newline="\n")
        mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
        try:
            app = create_app(Ctx(config, MemorySecrets()))  # type: ignore[arg-type]
            with app_client(app, sqlite_settings) as client:
                html = client.get("/login").text
        finally:
            mode.reset()
        assert 'class="auth-card"' in html and "auth-endorse" in html
        assert spark.__version__ not in html


class TestBolt:
    def test_the_bolt_is_the_polygon_of_the_loading_page_unchanged(self) -> None:
        loading = (TEMPLATES / "loading.html").read_text(encoding="utf-8")
        bolt = (STATIC / "img" / "spark-bolt.svg").read_text(encoding="utf-8")
        (splash,) = POLYGON.findall(loading)
        (saved,) = POLYGON.findall(bolt)
        assert saved == splash == "282,61 143,241 241,241 200,451 369,225 271,225"
        # The same fill, and nothing of the tile.
        assert re.findall(r'stop-color="(#\w+)"', bolt) == re.findall(
            r'stop-color="(#\w+)"', loading
        )
        assert "<rect" not in bolt
        assert re.search(r'fill="url\(#bolt-grad\)"', bolt)

    def test_the_artwork_is_cropped_to_the_bolt(self) -> None:
        bolt = (STATIC / "img" / "spark-bolt.svg").read_text(encoding="utf-8")
        x, y, width, height = map(float, re.search(r'viewBox="([^"]+)"', bolt).group(1).split())  # type: ignore[union-attr]
        points = [tuple(map(float, p.split(","))) for p in POLYGON.search(bolt).group(1).split()]  # type: ignore[union-attr]
        assert x <= min(px for px, _ in points) and x + width >= max(px for px, _ in points)
        assert y <= min(py for _, py in points) and y + height >= max(py for _, py in points)
        assert width == height  # a square slot, with the bolt centred in it
        assert y == min(py for _, py in points)

    def test_the_tile_stays_the_favicon_and_app_icon(self) -> None:
        brand = (TEMPLATES / "_brand.html").read_text(encoding="utf-8")
        assert "/static/img/cognisn-mark.svg" in brand
        assert "cognisn-appicon-180.png" in brand


class TestLoadingPage:
    def test_the_splash_follows_the_standard_and_keeps_its_motion(self) -> None:
        html = render("loading.html")
        assert '<h1 class="auth-name' in html and ">SPARK</h1>" in html
        assert '<p class="auth-descriptor">Secure Personal AI Research Kit</p>' in html
        assert "A product of" in html and f"&copy; {YEAR} Cognisn Pty Ltd" in html
        assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
        assert "splash-progress-fill" in html and "animation: splash-pulse" in html
        assert "@media (prefers-reduced-motion: reduce)" in html
        assert "9.8.7-test" not in html
        assert html.index("data-bs-theme") < html.index('rel="stylesheet"')


class TestFonts:
    def test_inter_is_self_hosted_in_four_weights(self) -> None:
        css = (STATIC / "css" / "chrome.css").read_text(encoding="utf-8")
        for weight in (400, 500, 600, 700):
            assert f"../fonts/inter-latin-{weight}.woff2" in css
            font = STATIC / "fonts" / f"inter-latin-{weight}.woff2"
            assert font.read_bytes()[:4] == b"wOF2", font
        assert "http" not in css.split("@font-face")[1].split("}")[0]
        assert "SIL OPEN FONT LICENSE" in (STATIC / "fonts" / "Inter-OFL.txt").read_text(
            encoding="utf-8"
        )

    def test_inter_is_declared_once_and_used_only_by_the_frame_header_and_footer(self) -> None:
        for name in ("base.css", "cognisn.css", "spark.css", "connections.css"):
            css = (STATIC / "css" / name).read_text(encoding="utf-8")
            assert "font-family: 'Inter'" not in css, name
        css = (STATIC / "css" / "chrome.css").read_text(encoding="utf-8")
        for selector in ("body.auth-page {", ".app-navbar .brand-product {", ".app-footer {"):
            block = css[css.index(selector) :].split("}")[0]
            assert "var(--chrome-font)" in block, selector
