"""User, role, invitation and API key administration in server mode (``spark.users.admin``).

The real app on a PostgreSQL clone at head with synthetic users. Guards under test:

* every administration screen and action needs ``spark.users.admin``: a User
  and an Auditor get 403, a signed-out page load goes to sign-in;
* warden's refusal to leave no active administrator is shown clearly and
  changes nothing;
* without a mail service, an invitation or reset link is shown to the
  administrator once, on a page that is never cached, and works.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.identity import UserStatus

from spark.identity.permissions import ADMINISTRATOR, API_KEYS, AUDITOR, POWER_USER, USER
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    code,
    field,
    message_of,
    plain_client,
    post_form,
    secret_on,
    server_db,
)
from tests.identity_web_helpers import (
    PUBLIC,
    another_client,
    make_user,
    runtime,
    sign_in,
)

pytestmark = pytest.mark.postgres

ADMIN = "admin@example.com"
ADA = "ada@example.com"
GRACE = "grace@example.com"
NEW_PASSWORD = "a-new-long-password-dev-only"

SCREENS = ["/admin/users", "/admin/invitations", "/admin/api-keys"]


@pytest.fixture
def admin(plain_client: TestClient) -> TestClient:
    make_user(plain_client, ADMIN, ADMINISTRATOR)
    assert sign_in(plain_client, ADMIN).status_code == 303
    return plain_client


def _identity(client: TestClient) -> Any:
    return runtime(client).identity


def _call(client: TestClient, fn: Any) -> Any:
    return client.portal.call(fn)  # type: ignore[attr-defined]


def _page(client: TestClient, path: str) -> Any:
    page = client.get(path, headers=NAVIGATE, follow_redirects=False)
    assert page.status_code == 200, (path, page.status_code, page.text[:300])
    return page


def _post(client: TestClient, path: str, **fields: Any) -> Any:
    return client.post(path, data=fields, follow_redirects=False)


def _user(client: TestClient, user_id: uuid.UUID) -> Any:
    async def get() -> Any:
        return await _identity(client).users.get(user_id)

    return _call(client, get)


def _roles(client: TestClient, user_id: uuid.UUID) -> list[str]:
    async def get() -> list[str]:
        roles: list[str] = await _identity(client).roles.roles_of(user_id)
        return roles

    result: list[str] = _call(client, get)
    return result


def _link_on(page: Any, path: str) -> str:
    match = re.search(
        rf'<code id="mailed-link"[^>]*>{re.escape(PUBLIC + path)}\?token=([^<]+)</code>', page.text
    )
    assert match, page.text
    return f"{path}?token={match.group(1)}"


class TestAccess:
    @pytest.mark.parametrize("role", [USER, POWER_USER, AUDITOR])
    def test_other_roles_are_refused(self, plain_client: TestClient, role: str) -> None:
        user_id = make_user(plain_client, ADA, role)
        sign_in(plain_client, ADA)
        for path in SCREENS + [f"/admin/users/{user_id}"]:
            assert plain_client.get(path, headers=NAVIGATE).status_code == 403, path
        for path in (
            "/admin/users",
            "/admin/invitations",
            f"/admin/users/{user_id}/status",
            f"/admin/users/{user_id}/roles",
            f"/admin/users/{user_id}/totp-reset",
            f"/admin/users/{user_id}/password-reset",
        ):
            assert (
                _post(plain_client, path, status="suspended", role=ADMINISTRATOR).status_code == 403
            ), path
        assert _roles(plain_client, user_id) == [role]

    def test_signed_out_goes_to_sign_in(self, plain_client: TestClient) -> None:
        for path in SCREENS:
            resp = plain_client.get(path, headers=NAVIGATE, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/sign-in")

    def test_an_administrator_reaches_every_screen(self, admin: TestClient) -> None:
        for path in SCREENS:
            _page(admin, path)


class TestUsers:
    def test_the_list_pages(self, admin: TestClient) -> None:
        for n in range(30):
            make_user(admin, f"user{n:02d}@example.com")
        first = _page(admin, "/admin/users")
        assert first.text.count('href="/admin/users/') == 25
        after = re.search(r'href="/admin/users\?after=([^"&]+)', first.text)
        assert after
        second = _page(admin, f"/admin/users?after={after.group(1)}")
        assert second.text.count('href="/admin/users/') == 6  # 31 users in all
        assert "user29@example.com" in second.text

    def test_create_a_user_with_a_password_and_roles(self, admin: TestClient) -> None:
        resp = _post(
            admin,
            "/admin/users",
            email=ADA,
            display_name="Ada",
            method="password_and_sso",
            password=NEW_PASSWORD,
            roles=[POWER_USER],
        )
        assert resp.status_code == 303 and resp.headers["location"].startswith("/admin/users/")
        assert sign_in(another_client(admin), ADA, NEW_PASSWORD).status_code == 303
        duplicate = _post(
            admin, "/admin/users", email=ADA, method="password_and_sso", password=NEW_PASSWORD
        )
        assert duplicate.status_code == 400 and "already exists" in message_of(duplicate)

    def test_suspend_reactivate_and_deactivate(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        ada = another_client(admin)
        sign_in(ada, ADA)
        assert _post(admin, f"/admin/users/{ada_id}/status", status="suspended").status_code == 303
        assert _user(admin, ada_id).status is UserStatus.SUSPENDED
        refused = ada.get("/conversations", headers=NAVIGATE, follow_redirects=False)
        assert refused.status_code == 303
        assert _post(admin, f"/admin/users/{ada_id}/status", status="active").status_code == 303
        assert (
            _post(admin, f"/admin/users/{ada_id}/status", status="deactivated").status_code == 303
        )
        assert _user(admin, ada_id).status is UserStatus.DEACTIVATED
        assert _post(admin, f"/admin/users/{ada_id}/status", status="invited").status_code == 400

    def test_the_last_administrator_cannot_be_suspended(self, admin: TestClient) -> None:
        admin_id = _identity_id(admin, ADMIN)
        resp = _post(admin, f"/admin/users/{admin_id}/status", status="suspended")
        assert resp.status_code == 409
        assert "last active administrator" in message_of(resp)
        assert _user(admin, admin_id).status is UserStatus.ACTIVE

    def test_reset_totp(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        ada = another_client(admin)
        sign_in(ada, ADA)
        page = ada.post("/account/mfa", follow_redirects=False)
        ada.post("/account/mfa/confirm", data={"code": code(secret_on(page))})

        async def enrolled() -> bool:
            result: bool = await _identity(admin).mfa.has_confirmed_totp(ada_id)
            return result

        assert _call(admin, enrolled)
        resp = _post(admin, f"/admin/users/{ada_id}/totp-reset")
        assert resp.status_code == 303
        assert not _call(admin, enrolled)

    def test_send_a_password_reset_shows_the_link_once(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        resp = _post(admin, f"/admin/users/{ada_id}/password-reset")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        link = _link_on(resp, "/auth/reset")
        assert "mailed-link" not in _page(admin, f"/admin/users/{ada_id}").text
        browser = TestClient(admin.app, base_url=PUBLIC)
        browser.portal = admin.portal  # type: ignore[attr-defined]
        page = browser.get(link)
        done = post_form(
            browser,
            "/auth/reset",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert done.status_code == 303
        assert sign_in(browser, ADA, NEW_PASSWORD).status_code == 303

    def test_an_unknown_user_is_not_found(self, admin: TestClient) -> None:
        assert admin.get(f"/admin/users/{uuid.uuid4()}", headers=NAVIGATE).status_code == 404
        assert admin.get("/admin/users/not-an-id", headers=NAVIGATE).status_code == 404


def _identity_id(client: TestClient, email: str) -> uuid.UUID:
    async def get() -> uuid.UUID:
        user = await _identity(client).users.get_by_email(email)
        assert user is not None
        found: uuid.UUID = user.id
        return found

    result: uuid.UUID = _call(client, get)
    return result


class TestRoles:
    def test_assign_and_revoke(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        assert _post(admin, f"/admin/users/{ada_id}/roles", role=POWER_USER).status_code == 303
        assert _roles(admin, ada_id) == [POWER_USER, USER]
        resp = _post(admin, f"/admin/users/{ada_id}/roles/{USER}/revoke")
        assert resp.status_code == 303
        assert _roles(admin, ada_id) == [POWER_USER]
        assert _post(admin, f"/admin/users/{ada_id}/roles", role="no_such_role").status_code == 400

    def test_the_last_administrator_keeps_the_role(self, admin: TestClient) -> None:
        admin_id = _identity_id(admin, ADMIN)
        resp = _post(admin, f"/admin/users/{admin_id}/roles/{ADMINISTRATOR}/revoke")
        assert resp.status_code == 409
        assert "last active administrator" in message_of(resp)
        assert _roles(admin, admin_id) == [ADMINISTRATOR]

    def test_the_detail_page_lists_roles_with_their_forms(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        page = _page(admin, f"/admin/users/{ada_id}")
        assert f'action="/admin/users/{ada_id}/roles/{USER}/revoke"' in page.text
        assert f'action="/admin/users/{ada_id}/roles"' in page.text


class TestInvitations:
    def test_invite_accept_resend_and_revoke(self, admin: TestClient) -> None:
        resp = _post(
            admin,
            "/admin/invitations",
            email=ADA,
            display_name="Ada",
            method="password_and_sso",
            roles=[POWER_USER],
        )
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
        first = _link_on(resp, "/auth/invitation")
        pending = _page(admin, "/admin/invitations")
        assert ADA in pending.text and "mailed-link" not in pending.text
        ada_id = _identity_id(admin, ADA)
        resent = _post(admin, f"/admin/invitations/{ada_id}/resend")
        assert resent.status_code == 200
        second = _link_on(resent, "/auth/invitation")
        assert second != first

        browser = TestClient(admin.app, base_url=PUBLIC)
        browser.portal = admin.portal  # type: ignore[attr-defined]
        fields = {"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
        page = browser.get(first)
        old = post_form(browser, "/auth/invitation", page, token=field(page, "token"), **fields)
        assert old.status_code == 401
        page = browser.get(second)
        ok = post_form(browser, "/auth/invitation", page, token=field(page, "token"), **fields)
        assert ok.status_code == 303
        assert _roles(admin, ada_id) == [POWER_USER]
        assert ADA not in _page(admin, "/admin/invitations").text

    def test_revoke_a_pending_invitation(self, admin: TestClient) -> None:
        _post(admin, "/admin/invitations", email=GRACE, method="password_and_sso")
        grace_id = _identity_id(admin, GRACE)
        resp = _post(admin, f"/admin/invitations/{grace_id}/revoke")
        assert resp.status_code == 303
        assert _user(admin, grace_id) is None
        again = _post(admin, f"/admin/invitations/{grace_id}/revoke")
        assert again.status_code == 409

    def test_an_address_in_use_is_refused(self, admin: TestClient) -> None:
        resp = _post(admin, "/admin/invitations", email=ADMIN, method="password_and_sso")
        assert resp.status_code == 400 and "mailed-link" not in resp.text


class TestApiKeys:
    def test_list_every_users_keys_and_revoke_one(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        keys = _identity(admin).api_keys

        async def create() -> Any:
            return await keys.create(ada_id, "ada laptop", {API_KEYS}, actor="test")

        created = _call(admin, create)
        page = _page(admin, "/admin/api-keys")
        assert ADA in page.text and "ada laptop" in page.text and created.key not in page.text
        resp = _post(admin, f"/admin/api-keys/{created.api_key.id}/revoke")
        assert resp.status_code == 303
        bearer = TestClient(admin.app, base_url=PUBLIC)
        bearer.portal = admin.portal  # type: ignore[attr-defined]
        refused = bearer.get("/auth/session", headers={"Authorization": f"Bearer {created.key}"})
        assert refused.status_code == 401
        assert _post(admin, f"/admin/api-keys/{uuid.uuid4()}/revoke").status_code == 404
