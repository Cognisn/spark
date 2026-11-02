"""The signed-in user's own screens in server mode: profile, password, TOTP and API keys.

The real app on a PostgreSQL clone at head with synthetic users. Guards under test:

* every screen needs a signed-in user (a page load without one goes to sign-in);
* own API keys need ``spark.api_keys``; a key's scopes may not exceed the
  permissions of the request creating it (so a key cannot mint a wider key),
  the key is shown once, works as a bearer and stops working once revoked,
  and nobody revokes another user's key here;
* the menu shows each entry only to a role holding its permission, and
  personal mode's menu is unchanged.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark.identity.permissions import ADMINISTRATOR, API_KEYS, AUDITOR, USER, role_permissions
from spark.web.menu import identity_menu
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    code,
    field,
    form_token,
    message_of,
    plain_client,
    post_form,
    recovery_codes_on,
    secret_on,
    server_db,
)
from tests.identity_web_helpers import PASSWORD, another_client, make_user, sign_in

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
NEW_PASSWORD = "another-long-password-dev-only"
# A permission a User holds besides managing their own keys.
TOOL = sorted(role_permissions(USER) - {API_KEYS})[0]


@pytest.fixture
def ada(plain_client: TestClient) -> TestClient:
    make_user(plain_client, ADA, USER)
    assert sign_in(plain_client, ADA).status_code == 303
    return plain_client


def _page(client: TestClient, path: str) -> Any:
    page = client.get(path, headers=NAVIGATE, follow_redirects=False)
    assert page.status_code == 200, (path, page.status_code, page.text[:300])
    return page


def _post(client: TestClient, path: str, **fields: Any) -> Any:
    return client.post(path, data=fields, follow_redirects=False)


def _create_key(client: TestClient, *scopes: str, name: str = "laptop") -> Any:
    return _post(client, "/account/api-keys", name=name, scopes=list(scopes))


def _key_on(page: Any) -> str:
    match = re.search(r'<code id="new-api-key"[^>]*>(wdn_[^<]+)</code>', page.text)
    assert match, page.text
    return match.group(1)


class TestProfile:
    def test_shows_the_email_and_roles(self, ada: TestClient) -> None:
        page = _page(ada, "/account")
        assert ADA in page.text and "user" in page.text
        assert 'action="/account/password"' in page.text

    def test_needs_a_signed_in_user(self, plain_client: TestClient) -> None:
        resp = plain_client.get("/account", headers=NAVIGATE, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/auth/sign-in")


class TestChangePassword:
    def test_a_wrong_current_password_is_refused(self, ada: TestClient) -> None:
        resp = _post(
            ada, "/account/password", current="wrong", password=NEW_PASSWORD, confirm=NEW_PASSWORD
        )
        assert resp.status_code == 400
        assert sign_in(another_client(ada), ADA).status_code == 303

    def test_changing_keeps_this_session_and_ends_the_others(self, ada: TestClient) -> None:
        other = another_client(ada)
        assert sign_in(other, ADA).status_code == 303
        resp = _post(
            ada,
            "/account/password",
            current=PASSWORD,
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/account?done=password"
        assert _page(ada, "/account?done=password").status_code == 200
        stale = other.get("/conversations", headers=NAVIGATE, follow_redirects=False)
        assert stale.status_code == 303
        third = another_client(ada)
        assert sign_in(third, ADA).status_code == 401
        assert sign_in(third, ADA, NEW_PASSWORD).status_code == 303


class TestOwnEnrolment:
    def test_enrol_while_signed_in_and_see_the_codes_once(self, ada: TestClient) -> None:
        page = _post(ada, "/account/mfa")
        assert page.status_code == 200
        assert 'src="data:image/svg+xml;base64,' in page.text
        secret = secret_on(page)
        wrong = _post(
            ada,
            "/account/mfa/confirm",
            code="000000" if code(secret) != "000000" else "111111",
            secret=field(page, "secret"),
            uri=field(page, "uri"),
        )
        assert wrong.status_code == 400 and secret_on(wrong) == secret
        done = _post(ada, "/account/mfa/confirm", code=code(secret))
        assert done.status_code == 200
        assert len(recovery_codes_on(done)) == 10
        assert done.headers["cache-control"] == "no-store"
        assert "recovery-codes" not in _page(ada, "/account").text
        # Enrolled: starting again is refused with a message, nothing changes.
        again = _post(ada, "/account/mfa")
        assert again.status_code == 303 and again.headers["location"] == "/account?done=mfa-exists"


class TestOwnApiKeys:
    def test_listed_only_for_a_role_holding_the_permission(self, plain_client: TestClient) -> None:
        make_user(plain_client, GRACE, AUDITOR)
        sign_in(plain_client, GRACE)
        resp = plain_client.get("/account/api-keys", headers=NAVIGATE)
        assert resp.status_code == 403
        assert _create_key(plain_client, API_KEYS).status_code == 403

    def test_a_key_is_shown_once_works_as_a_bearer_and_is_revoked(self, ada: TestClient) -> None:
        created = _create_key(ada, API_KEYS, TOOL)
        assert created.status_code == 200
        assert created.headers["cache-control"] == "no-store"
        key = _key_on(created)
        listing = _page(ada, "/account/api-keys")
        assert key not in listing.text and key[:8] in listing.text
        bearer = TestClient(ada.app, base_url=ada.base_url)
        bearer.portal = ada.portal  # type: ignore[attr-defined]
        me = bearer.get("/auth/session", headers={"Authorization": f"Bearer {key}"})
        assert me.status_code == 200
        assert me.json()["via"] == "api_key"
        assert set(me.json()["permissions"]) == {API_KEYS, TOOL}
        key_id = re.search(r'action="/account/api-keys/([0-9a-f-]+)/revoke"', listing.text)
        assert key_id
        revoked = _post(ada, f"/account/api-keys/{key_id.group(1)}/revoke")
        assert revoked.status_code == 303
        refused = bearer.get("/auth/session", headers={"Authorization": f"Bearer {key}"})
        assert refused.status_code == 401

    def test_scopes_cannot_exceed_the_users_permissions(self, ada: TestClient) -> None:
        resp = _create_key(ada, API_KEYS, "spark.users.admin")
        assert resp.status_code == 400
        assert 'id="new-api-key"' not in resp.text

    def test_a_key_cannot_mint_a_wider_key(self, ada: TestClient) -> None:
        key = _key_on(_create_key(ada, API_KEYS))
        bearer = TestClient(ada.app, base_url=ada.base_url)
        bearer.portal = ada.portal  # type: ignore[attr-defined]
        headers = {"Authorization": f"Bearer {key}"}
        wider = bearer.post(
            "/account/api-keys",
            data={"name": "wider", "scopes": [API_KEYS, TOOL]},
            headers=headers,
        )
        assert wider.status_code == 400
        assert 'id="new-api-key"' not in wider.text
        same = bearer.post(
            "/account/api-keys", data={"name": "same", "scopes": [API_KEYS]}, headers=headers
        )
        assert same.status_code == 200

    def test_another_users_key_is_not_found(self, ada: TestClient) -> None:
        grace_id = make_user(ada, GRACE, USER)
        keys = ada.app.state.identity_runtime.identity.api_keys  # type: ignore[attr-defined]

        async def create() -> Any:
            return (await keys.create(grace_id, "grace", {API_KEYS}, actor="test")).api_key.id

        grace_key = ada.portal.call(create)  # type: ignore[attr-defined]
        resp = _post(ada, f"/account/api-keys/{grace_key}/revoke")
        assert resp.status_code == 404

        async def still_live() -> bool:
            key = await keys.get(grace_key)
            return key is not None and key.revoked_at is None

        assert ada.portal.call(still_live)  # type: ignore[attr-defined]


class TestMenu:
    @pytest.mark.parametrize(
        ("role", "shown", "hidden"),
        [
            (USER, {"/account", "/account/api-keys"}, {"/admin/users", "/audit"}),
            (AUDITOR, {"/account", "/audit"}, {"/admin/users", "/account/api-keys"}),
            (
                ADMINISTRATOR,
                {"/account", "/account/api-keys", "/admin/users", "/admin/invitations", "/audit"},
                set(),
            ),
        ],
    )
    def test_entries_follow_the_role(
        self, plain_client: TestClient, role: str, shown: set[str], hidden: set[str]
    ) -> None:
        make_user(plain_client, GRACE, role)
        sign_in(plain_client, GRACE)
        page = _page(plain_client, "/account")
        links = set(re.findall(r'class="dropdown-item" href="([^"]+)"', page.text))
        assert shown <= links, links
        assert not hidden & links, links
        assert 'action="/auth/sign-out"' in page.text

    def test_personal_mode_has_no_entries(self) -> None:
        request = MagicMock()
        request.app.state.identity_attached = False
        assert identity_menu(request) == []
