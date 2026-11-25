"""SPARK's navigation: the Account menu, the Administration area, old URLs and branding.

Guards under test:

* the Administration area (its link and ``/admin``) is shown only to a user
  holding an administrative permission, each entry only to a holder of the
  permission its page requires; personal mode has none;
* every page the old Account menu and instance settings page offered is
  reachable from the new navigation, and no setting is lost in the split;
* every old URL redirects to its new one, keeping the query string;
* the Cognisn logo, favicon and spinner are served.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import mode
from spark.identity.permissions import (
    ADMINISTRATOR,
    AUDIT_READ,
    AUDITOR,
    POWER_USER,
    USER,
    role_permissions,
)
from spark.mode import Mode, RuntimeSettings
from spark.web import menu
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.identity_helpers import identity_for
from tests.identity_screen_helpers import NAVIGATE, plain_client, server_db  # noqa: F401
from tests.identity_web_helpers import make_user, sign_in
from tests.test_instance_settings_web import Ctx, MemorySecrets
from tests.web_helpers import sign_in as personal_sign_in

# The Account menu before 0.3.0's reorganisation, and where each entry lives now.
OLD_ACCOUNT_MENU = {
    "/account": "/account",
    "/account/api-keys": "/account/api-keys",
    "/admin/users": "/admin/users",
    "/admin/invitations": "/admin/invitations",
    "/admin/api-keys": "/admin/api-keys",
    "/audit": "/admin/audit",
    "/admin/usage": "/admin/usage",
    "/admin/connections": "/admin/connections",
    "/admin/bundles": "/admin/bundles",
}

# The pages instance settings and preferences moved to.
SETTINGS_PAGES = ["/admin/settings", "/admin/tools", "/admin/mail", "/admin/security"]

STATIC_ASSETS = [
    "/static/img/cognisn-mark.svg",
    "/static/img/cognisn-mark-64.png",
    "/static/img/cognisn-appicon-180.png",
    "/static/img/cognisn-logo-dark-text-96.png",
    "/static/img/cognisn-logo-light-text-96.png",
    "/static/img/cognisn-logo-dark-text-192.png",
    "/static/img/cognisn-logo-light-text-192.png",
    "/static/img/cognisn-spinner.gif",
]


def _request(monkeypatch: pytest.MonkeyPatch, *roles: str, attached: bool = True) -> Any:
    request = MagicMock()
    request.app.state.identity_attached = attached
    identity = identity_for(*roles) if roles else None
    monkeypatch.setattr(menu, "current_identity", lambda _request: identity)
    return request


def _admin_hrefs(groups: list[menu.MenuGroup]) -> list[str]:
    return [entry.href for group in groups for entry in group.entries]


def _hrefs(page: Any) -> set[str]:
    return set(re.findall(r'href="([^"#?]+)', page.text))


# -- The menus, without a server ------------------------------------------------------


class TestMenus:
    def test_administrator_sees_every_area_in_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        groups = menu.admin_menu(_request(monkeypatch, ADMINISTRATOR))
        assert [g.label for g in groups] == ["Users and access", ""]
        assert _admin_hrefs(groups) == [
            "/admin/users",
            "/admin/invitations",
            "/admin/sign-in",
            "/admin/sso",
            "/admin/api-keys",
            "/admin/connections",
            "/admin/tools",
            "/admin/mail",
            "/admin/security",
            "/admin/settings",
            "/admin/usage",
            "/admin/audit",
            "/admin/bundles",
        ]

    def test_auditor_sees_only_the_audit_trail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        groups = menu.admin_menu(_request(monkeypatch, AUDITOR))
        assert _admin_hrefs(groups) == ["/admin/audit"]

    @pytest.mark.parametrize("role", [USER, POWER_USER])
    def test_users_have_no_administration(self, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
        assert menu.admin_menu(_request(monkeypatch, role)) == []

    def test_each_entry_needs_its_own_permission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for group in menu.ADMIN_GROUPS:
            for entry in group.entries:
                assert entry.permission is not None, entry
                identity = replace(
                    identity_for(), roles=frozenset(), permissions=frozenset({entry.permission})
                )
                request = MagicMock()
                request.app.state.identity_attached = True
                monkeypatch.setattr(menu, "current_identity", lambda _r, i=identity: i)
                shown = _admin_hrefs(menu.admin_menu(request))
                assert entry.href in shown
                for other in menu.ADMIN_GROUPS:
                    for o in other.entries:
                        if o.permission != entry.permission:
                            assert o.href not in shown, (entry.href, o.href)

    def test_administrative_permissions(self) -> None:
        assert AUDIT_READ in menu.ADMIN_PERMISSIONS
        assert not menu.ADMIN_PERMISSIONS & role_permissions(POWER_USER)
        assert menu.ADMIN_PERMISSIONS <= role_permissions(ADMINISTRATOR)

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (USER, ["/account", "/account/api-keys", "/account/preferences"]),
            (AUDITOR, ["/account", "/account/preferences"]),
            (ADMINISTRATOR, ["/account", "/account/api-keys", "/account/preferences"]),
        ],
    )
    def test_account_menu_is_personal(
        self, monkeypatch: pytest.MonkeyPatch, role: str, expected: list[str]
    ) -> None:
        assert [e.href for e in menu.identity_menu(_request(monkeypatch, role))] == expected

    def test_no_page_is_lost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        request = _request(monkeypatch, ADMINISTRATOR)
        reachable = {e.href for e in menu.identity_menu(request)}
        reachable |= set(_admin_hrefs(menu.admin_menu(request)))
        assert set(OLD_ACCOUNT_MENU.values()) <= reachable
        assert set(SETTINGS_PAGES) | {"/account/preferences", "/admin/sso"} <= reachable

    def test_personal_mode_and_signed_out_have_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert menu.admin_menu(_request(monkeypatch, ADMINISTRATOR, attached=False)) == []
        assert menu.identity_menu(_request(monkeypatch, ADMINISTRATOR, attached=False)) == []
        assert menu.admin_menu(_request(monkeypatch)) == []
        assert menu.identity_menu(_request(monkeypatch)) == []


# -- Personal mode ---------------------------------------------------------------------


@pytest.fixture
def personal(tmp_path: Path, sqlite_settings: Any) -> Iterator[TestClient]:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8", newline="\n")
    mode.configure(RuntimeSettings(Mode.PERSONAL, sqlite_settings))
    try:
        app = create_app(Ctx(config, MemorySecrets()))  # type: ignore[arg-type]
        with app_client(app, sqlite_settings) as client:
            personal_sign_in(client)
            yield client
    finally:
        mode.reset()


class TestPersonal:
    def test_navigation_keeps_settings_and_usage(self, personal: TestClient) -> None:
        page = personal.get("/")
        assert page.status_code == 200
        links = _hrefs(page)
        assert {"/settings", "/admin/usage", "/conversations"} <= links
        assert "/admin" not in links
        assert "Administration" not in page.text

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("/admin", "/settings"),
            ("/account/preferences", "/settings"),
            ("/admin/settings", "/settings"),
            ("/admin/tools", "/settings#section-embedded-tools"),
            ("/admin/security", "/settings#section-security"),
            ("/admin/mail", "/settings"),
        ],
    )
    def test_server_pages_lead_to_settings(self, personal: TestClient, old: str, new: str) -> None:
        resp = personal.get(old, follow_redirects=False)
        assert resp.status_code in (303, 307), (old, resp.status_code)
        assert resp.headers["location"] == new

    def test_settings_sections_follow_the_server_grouping(self, personal: TestClient) -> None:
        text = personal.get("/settings").text
        order = [
            "section-connections",
            "section-embedded-tools",
            "section-mcp-servers",
            "section-security",
            "section-default_model",
            "section-conversation",
            "section-agents",
            "section-autonomous_actions",
            "section-voice",
            "section-interface",
            "section-database",
            "section-logging",
        ]
        positions = [text.index(f'id="{anchor}"') for anchor in order]
        assert positions == sorted(positions)

    def test_branding(self, personal: TestClient) -> None:
        for path in ("/", "/login"):
            text = personal.get(path).text
            assert 'alt="Cognisn"' in text, path
            assert 'rel="icon" href="/static/img/cognisn-mark.svg"' in text, path
            assert ">SPARK<" in text, path
        for asset in STATIC_ASSETS:
            resp = personal.get(asset)
            assert resp.status_code == 200, asset
            assert len(resp.content) < 128 * 1024, asset


# -- Server mode -----------------------------------------------------------------------


def _as(client: TestClient, role: str) -> TestClient:
    email = f"{role.replace('_', '-')}@example.com"
    make_user(client, email, role)
    assert sign_in(client, email).status_code == 303
    return client


def _get(client: TestClient, path: str) -> Any:
    return client.get(path, headers=NAVIGATE, follow_redirects=False)


@pytest.mark.postgres
class TestServer:
    def test_administrator_reaches_every_page_from_the_navigation(
        self, plain_client: TestClient
    ) -> None:
        admin = _as(plain_client, ADMINISTRATOR)
        home = _get(admin, "/")
        assert home.status_code == 200
        assert "/admin" in _hrefs(home)
        landing = _get(admin, "/admin")
        assert landing.status_code == 200
        targets = {href for href in _hrefs(landing) if href.startswith("/admin/")}
        assert set(_admin_hrefs(list(menu.ADMIN_GROUPS))) <= targets
        account = set(re.findall(r'class="dropdown-item" href="([^"]+)"', landing.text))
        assert {"/account", "/account/api-keys", "/account/preferences"} <= account
        for href in sorted(targets | account):
            page = _get(admin, href)
            assert page.status_code == 200, (href, page.status_code, page.text[:300])
            # Every administration page carries the side navigation.
            if href.startswith("/admin/"):
                assert 'aria-label="Administration"' in page.text, href

    @pytest.mark.parametrize("role", [USER, POWER_USER])
    def test_users_see_no_administration(self, plain_client: TestClient, role: str) -> None:
        user = _as(plain_client, role)
        home = _get(user, "/")
        assert home.status_code == 200
        assert "/admin" not in _hrefs(home)
        assert not {h for h in _hrefs(home) if h.startswith("/admin/")}
        assert ">Administration<" not in home.text
        assert _get(user, "/admin").status_code == 403
        for path in SETTINGS_PAGES:
            assert _get(user, path).status_code == 403, path

    def test_auditor_sees_only_the_audit_trail(self, plain_client: TestClient) -> None:
        auditor = _as(plain_client, AUDITOR)
        landing = _get(auditor, "/admin")
        assert landing.status_code == 200
        assert {h for h in _hrefs(landing) if h.startswith("/admin/")} == {"/admin/audit"}

    def test_signed_out_goes_to_sign_in(self, plain_client: TestClient) -> None:
        for path in ["/admin", *SETTINGS_PAGES, "/account/preferences"]:
            resp = _get(plain_client, path)
            assert resp.status_code == 303, path
            assert resp.headers["location"].startswith("/auth/sign-in"), path

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("/settings", "/admin/settings"),
            ("/settings?x=1", "/admin/settings?x=1"),
            ("/settings/preferences", "/account/preferences"),
            ("/audit", "/admin/audit"),
            ("/audit?outcome=success&actor=a", "/admin/audit?outcome=success&actor=a"),
            ("/audit/export?format=csv", "/admin/audit/export?format=csv"),
        ],
    )
    def test_old_urls_redirect(self, plain_client: TestClient, old: str, new: str) -> None:
        admin = _as(plain_client, ADMINISTRATOR)
        resp = _get(admin, old)
        assert resp.status_code in (303, 307), (old, resp.status_code)
        assert resp.headers["location"] == new

    def test_non_administrator_settings_go_to_preferences(self, plain_client: TestClient) -> None:
        user = _as(plain_client, USER)
        resp = _get(user, "/settings")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/account/preferences"
        assert _get(user, "/account/preferences").status_code == 200

    def test_no_setting_is_lost_in_the_split(self, plain_client: TestClient) -> None:
        from spark.instance_settings.registry import Scope
        from spark.web.endpoints import settings as settings_page

        admin = _as(plain_client, ADMINISTRATOR)
        shown: list[str] = []
        for path in SETTINGS_PAGES:
            page = _get(admin, path)
            assert page.status_code == 200, path
            shown += re.findall(r'data-key="([^"]+)"', page.text)
        view = settings_page._View(lambda key, default: default, {})
        expected = {
            field["key"]
            for section in settings_page._filter_sections(
                settings_page._build_sections(view), {Scope.INSTANCE}
            )
            + [settings_page._mail_section(view)]
            for group in section["groups"]
            for field in group["fields"]
        }
        assert expected <= set(shown)
        # Each setting is on one page only, so one page's save never overwrites another's.
        sectioned = [key for key in shown if key in expected]
        assert len(sectioned) == len(set(sectioned))

    def test_moved_anchors_are_forwarded(self, plain_client: TestClient) -> None:
        admin = _as(plain_client, ADMINISTRATOR)
        page = _get(admin, "/admin/settings").text
        for anchor, target in (
            ("section-mcp-servers", "/admin/tools"),
            ("section-embedded-tools", "/admin/tools"),
            ("section-mail", "/admin/mail"),
            ("section-security", "/admin/security"),
        ):
            assert f'"{anchor}": "{target}"' in page, anchor
