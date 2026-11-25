"""Sign-in settings on SPARK's screens (server mode, warden 0.2.0, rulings CS13 to CS18).

The real app on a PostgreSQL clone at head with synthetic users.

* Administration, Sign-in (``/admin/sign-in``): the default method and the allowed
  second factors, behind ``spark.users.admin``; single sign-on is chosen only once it is
  set up, passkeys only where they work, and at least one second factor stays allowed.
* A default change moves everyone who follows it: now when they hold what the new method
  needs, otherwise as a change waiting for them, and the page says how many of each.
* A user's own method, or following the default, with a pending change that can be
  resent or cancelled; the users list and the invitations show the method.
* The single sign-on page shows warden's numbered setup steps for the chosen provider.
* Account security shows the user's own method and any change waiting, and replaces
  recovery codes only after a second factor proved just now.

Every new route is checked for its permission and its CSRF token.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from warden.db import schema, table
from warden.identity import SecondFactorKind, SignInMethod

from spark.identity.permissions import ADMINISTRATOR, USER
from spark.web.endpoints.account import FRESH_FACTOR_NEEDED
from spark.web.security import CSRF_HEADER
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    message_of,
    plain_client,
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
LIN = "lin@example.com"
SIGN_IN_PATH = "/admin/sign-in"


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


def _settings(client: TestClient) -> Any:
    return _call(client, _identity(client).sign_in_settings)


def _state(client: TestClient, user_id: uuid.UUID) -> Any:
    return _call(client, lambda: _identity(client).sign_in_method(user_id))


def _save(client: TestClient, default: str, *factors: str) -> Any:
    return client.post(
        SIGN_IN_PATH,
        data={"default_method": default, "factors": list(factors or ("totp", "passkey"))},
        follow_redirects=False,
    )


def _text(page: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.text))


def _set_up_sso(client: TestClient) -> None:
    """Single sign-on set up through warden directly (the SSO screens have their own tests)."""

    async def set_up() -> None:
        sso = _identity(client).sso
        await sso.set_client_secret("synthetic-client-secret-dev-only", actor="test")
        await sso.settings.update(
            actor="test",
            enabled=True,
            issuer_url="https://idp.example.com/realms/spark",
            client_id="spark-dev-only",
        )

    _call(client, set_up)


# -- Access ---------------------------------------------------------------------------------


class TestAccess:
    def _routes(self, user_id: uuid.UUID) -> list[tuple[str, str]]:
        base = f"/admin/users/{user_id}/sign-in-method"
        return [
            ("GET", SIGN_IN_PATH),
            ("POST", SIGN_IN_PATH),
            ("POST", base),
            ("POST", base + "/resend"),
            ("POST", base + "/cancel"),
        ]

    def test_another_role_is_refused_everywhere(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        assert sign_in(plain_client, ADA).status_code == 303
        for method, path in self._routes(uuid.uuid4()):
            resp = plain_client.request(method, path, headers=NAVIGATE, follow_redirects=False)
            assert resp.status_code == 403, (method, path, resp.status_code)

    def test_a_refused_post_changes_nothing(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        sign_in(plain_client, ADA)
        before = _settings(plain_client)
        assert _save(plain_client, "password", "totp").status_code == 403
        assert _settings(plain_client) == before

    def test_signed_out_goes_to_sign_in(self, plain_client: TestClient) -> None:
        resp = plain_client.get(SIGN_IN_PATH, headers=NAVIGATE, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/sign-in")

    def test_the_routes_need_the_csrf_token(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        del admin.headers[CSRF_HEADER]
        for method, path in self._routes(ada):
            if method == "GET":
                continue
            resp = admin.post(path, data={"default_method": "password", "factors": ["totp"]})
            assert resp.status_code == 403, (path, resp.status_code)
        assert _settings(admin).default_sign_in_method is SignInMethod.PASSWORD_AND_SSO

    def test_recovery_codes_need_the_csrf_token_and_a_session(self, admin: TestClient) -> None:
        path = "/account/mfa/recovery-codes"
        signed_out = another_client(admin).post(path, headers={"Origin": PUBLIC})
        assert signed_out.status_code in (401, 403)
        del admin.headers[CSRF_HEADER]
        assert admin.post(path).status_code == 403

    def test_the_menu_lists_the_page(self, admin: TestClient) -> None:
        assert 'href="/admin/sign-in"' in _page(admin, "/admin").text


# -- The settings page ----------------------------------------------------------------------


class TestSettings:
    def test_the_defaults_are_shown(self, admin: TestClient) -> None:
        page = _page(admin, SIGN_IN_PATH)
        assert re.search(r'id="default-password_and_sso"[^>]*checked', page.text)
        assert re.search(r'id="factor-totp"[^>]*checked', page.text)
        for label in ("Password + MFA", "Passkey", "SSO", "Password + MFA or SSO"):
            assert f"<strong>{label}</strong>" in page.text

    def test_sso_cannot_be_chosen_until_it_is_set_up(self, admin: TestClient) -> None:
        page = _page(admin, SIGN_IN_PATH)
        assert re.search(r'id="default-sso_only"[^>]*disabled', page.text)
        refused = _save(admin, "sso_only")
        assert refused.status_code == 400 and "not switched on" in message_of(refused)
        assert _settings(admin).default_sign_in_method is SignInMethod.PASSWORD_AND_SSO

    def test_sso_can_be_chosen_once_it_is_set_up(self, admin: TestClient) -> None:
        _set_up_sso(admin)
        assert not re.search(r'id="default-sso_only"[^>]*disabled', _page(admin, SIGN_IN_PATH).text)

    def test_an_unknown_method_is_refused(self, admin: TestClient) -> None:
        assert _save(admin, "carrier-pigeon").status_code == 400

    def test_at_least_one_second_factor_stays_allowed(self, admin: TestClient) -> None:
        refused = admin.post(
            SIGN_IN_PATH, data={"default_method": "password_and_sso"}, follow_redirects=False
        )
        assert refused.status_code == 400
        assert message_of(refused) == "Allow at least one second factor."
        assert SecondFactorKind.TOTP in _settings(admin).allowed_second_factors

    def test_the_allowed_factors_are_saved(self, admin: TestClient) -> None:
        done = _save(admin, "password_and_sso", "totp")
        assert done.status_code == 200 and 'id="sign-in-result"' in done.text
        assert _settings(admin).allowed_second_factors == {SecondFactorKind.TOTP}
        assert not re.search(r'id="factor-passkey"[^>]*checked', _page(admin, SIGN_IN_PATH).text)

    def test_the_sso_guard_lists_who_depends_on_it(self, admin: TestClient) -> None:
        async def make() -> uuid.UUID:
            user = await _identity(admin).users.create(
                LIN, actor="test", sign_in_method=SignInMethod.SSO_ONLY
            )
            return user.id

        lin = _call(admin, make)
        page = _page(admin, SIGN_IN_PATH)
        listed = re.search(r'<ul class="mb-0" id="sso-dependants-list">(.*?)</ul>', page.text, re.S)
        assert listed and f'<a href="/admin/users/{lin}">{LIN}</a>' in listed.group(1)


# -- A default change -----------------------------------------------------------------------


class TestDefaultChange:
    def test_a_default_that_everyone_holds_moves_them_now(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        done = _save(admin, "password")
        assert done.status_code == 200
        assert re.search(r'id="result-changed">2 people moved now', done.text)
        assert re.search(r'id="result-pending">0 people will move', done.text)
        assert _state(admin, ada).effective is SignInMethod.PASSWORD
        assert _settings(admin).default_sign_in_method is SignInMethod.PASSWORD

    def test_a_default_they_lack_makes_pending_changes_and_shows_the_links(
        self, admin: TestClient
    ) -> None:
        ada = make_user(admin, ADA, USER)
        done = _save(admin, "passkey_only")
        assert done.status_code == 200, done.text[:300]
        assert re.search(r'id="result-pending">2 people will move', done.text)
        assert re.search(r'id="result-changed">0 people moved now', done.text)
        # The method in force keeps working while the change waits.
        state = _state(admin, ada)
        assert state.effective is SignInMethod.PASSWORD_AND_SSO
        assert state.pending is not None and state.pending.to_method is SignInMethod.PASSKEY_ONLY
        # No mail service: each link is shown once, on a page that is never cached.
        assert "no-store" in done.headers["cache-control"]
        assert done.text.count("/auth/passkey-registration?token=") == 2
        assert f'<a href="/admin/users/{ada}">{ADA}</a>' in done.text
        assert "/auth/passkey-registration?token=" not in _page(admin, SIGN_IN_PATH).text

    def test_a_change_of_the_factors_alone_moves_nobody(self, admin: TestClient) -> None:
        make_user(admin, ADA, USER)
        done = _save(admin, "password_and_sso", "totp")
        assert 'id="result-same"' in done.text and 'id="result-pending"' not in done.text


# -- A user's own method --------------------------------------------------------------------


class TestUserMethod:
    def test_the_page_shows_the_default_and_offers_it_first(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        page = _page(admin, f"/admin/users/{ada}")
        assert re.search(
            r'id="method-setting">Uses the instance default \(Password \+ MFA\)', page.text
        )
        choice = page.text.split('id="sign-in-method-choice"')[1].split("</select>")[0]
        options = re.findall(r'<option value="([a-z_]+)"', choice)
        assert options[0] == "default"
        assert "sso_only" not in options  # not set up

    def test_a_method_they_hold_applies_at_once_and_back_to_the_default(
        self, admin: TestClient
    ) -> None:
        ada = make_user(admin, ADA, USER)
        resp = _post(admin, f"/admin/users/{ada}/sign-in-method", method="password")
        assert resp.status_code == 303 and resp.headers["location"].endswith("?done=method")
        state = _state(admin, ada)
        assert state.setting is SignInMethod.PASSWORD and state.effective is SignInMethod.PASSWORD
        assert "Their own method" in _page(admin, f"/admin/users/{ada}").text
        back = _post(admin, f"/admin/users/{ada}/sign-in-method", method="default")
        assert back.status_code == 303
        assert _state(admin, ada).setting is None

    def test_a_method_they_lack_waits_and_can_be_resent_then_cancelled(
        self, admin: TestClient
    ) -> None:
        ada = make_user(admin, ADA, USER)
        resp = _post(admin, f"/admin/users/{ada}/sign-in-method", method="passkey_only")
        assert resp.status_code == 303 and resp.headers["location"].endswith("?done=method-pending")
        page = _page(admin, f"/admin/users/{ada}")
        assert 'id="method-pending"' in page.text
        assert "It completes when they register a passkey" in page.text
        assert f'action="/admin/users/{ada}/sign-in-method/resend"' in page.text
        assert f'action="/admin/users/{ada}/sign-in-method/cancel"' in page.text
        # Resend: a fresh link, shown once without a mail service.
        sent = _post(admin, f"/admin/users/{ada}/sign-in-method/resend")
        assert sent.status_code == 200 and "/auth/passkey-registration?token=" in sent.text
        assert "no-store" in sent.headers["cache-control"]
        # Cancel: the user stays as they were.
        cancelled = _post(admin, f"/admin/users/{ada}/sign-in-method/cancel")
        assert cancelled.status_code == 303
        state = _state(admin, ada)
        assert state.pending is None and state.effective is SignInMethod.PASSWORD_AND_SSO
        assert 'id="method-pending"' not in _page(admin, f"/admin/users/{ada}").text

    def test_nothing_to_resend_or_cancel_is_refused_in_plain_words(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        for action in ("resend", "cancel"):
            resp = _post(admin, f"/admin/users/{ada}/sign-in-method/{action}")
            assert resp.status_code == 409
            assert message_of(resp) == "No sign-in method change is waiting for this person."

    def test_an_unknown_method_is_refused(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        resp = _post(admin, f"/admin/users/{ada}/sign-in-method", method="carrier-pigeon")
        assert resp.status_code == 400
        assert _state(admin, ada).setting is None

    def test_an_unknown_user_is_not_found(self, admin: TestClient) -> None:
        missing = uuid.uuid4()
        for action in ("", "/resend", "/cancel"):
            resp = _post(admin, f"/admin/users/{missing}/sign-in-method{action}", method="password")
            assert resp.status_code == 404

    def test_the_list_shows_the_method_in_force_and_what_is_pending(
        self, admin: TestClient
    ) -> None:
        ada = make_user(admin, ADA, USER)
        _post(admin, f"/admin/users/{ada}/sign-in-method", method="passkey_only")
        users = _text(_page(admin, "/admin/users"))
        assert "Password + MFA" in users and "moving to Passkey" in users
        assert "(default)" in users  # the administrator follows the default

    def test_sso_is_offered_once_it_is_set_up(self, admin: TestClient) -> None:
        _set_up_sso(admin)
        ada = make_user(admin, ADA, USER)
        assert '<option value="sso_only"' in _page(admin, f"/admin/users/{ada}").text
        resp = _post(admin, f"/admin/users/{ada}/sign-in-method", method="sso_only")
        assert resp.status_code == 303
        assert _state(admin, ada).pending is not None  # waits for the first sign-in through it


# -- Invitations ----------------------------------------------------------------------------


class TestInvitations:
    def test_the_form_offers_the_default_first(self, admin: TestClient) -> None:
        page = _page(admin, "/admin/invitations")
        options = re.findall(r'<option value="([a-z_]+)"', page.text.split('id="method"')[1])
        assert options[0] == "default" and "password" in options

    def test_an_invitee_follows_the_default_or_has_their_own(self, admin: TestClient) -> None:
        for email, choice in ((GRACE, "default"), (LIN, "password")):
            resp = _post(admin, "/admin/invitations", email=email, method=choice)
            assert resp.status_code == 200, resp.text[:300]

        async def own() -> dict[str, Any]:
            found = {}
            for email in (GRACE, LIN):
                user = await _identity(admin).users.get_by_email(email)
                found[email] = user.own_sign_in_method
            return found

        assert _call(admin, own) == {GRACE: None, LIN: SignInMethod.PASSWORD}
        assert "(default)" in _text(_page(admin, "/admin/invitations"))

    def test_an_unknown_method_is_refused(self, admin: TestClient) -> None:
        resp = _post(admin, "/admin/invitations", email=GRACE, method="carrier-pigeon")
        assert resp.status_code == 400


# -- The single sign-on page ----------------------------------------------------------------


class TestSetupSteps:
    def test_the_steps_carry_the_callback_and_copy_buttons(self, admin: TestClient) -> None:
        page = _page(admin, "/admin/sso?preset=okta")
        callback = f"{PUBLIC}/identity/auth/sso/callback"
        steps = re.search(r'<ol class="ps-3 mb-0" id="sso-steps">(.*?)</ol>', page.text, re.S)
        assert steps, page.text[:400]
        assert callback in steps.group(1)
        assert 'id="sso-step-1"' in steps.group(1)
        assert re.search(r'data-copy="#sso-value-\d+-callback_uri"', steps.group(1))
        # Each value to copy back sits beside the setting it fills.
        assert 'href="#client_id"' in steps.group(1) and 'href="#client_secret"' in steps.group(1)
        assert re.search(r"From step \d+ of the steps above", page.text)
        assert '<option value="okta" selected>' in page.text

    def test_an_unknown_provider_gets_the_generic_steps(self, admin: TestClient) -> None:
        page = _page(admin, "/admin/sso?preset=nonsense")
        assert '<option value="generic" selected>' in page.text

    def test_the_provider_is_saved_with_the_settings(self, admin: TestClient) -> None:
        resp = _post(
            admin,
            "/admin/sso",
            issuer_url="https://idp.example.com/realms/spark",
            client_id="spark-dev-only",
            client_secret="",
            provider_preset="google",
        )
        assert resp.status_code == 303
        assert '<option value="google" selected>' in _page(admin, "/admin/sso").text

    def test_an_unknown_provider_is_not_saved(self, admin: TestClient) -> None:
        resp = _post(
            admin,
            "/admin/sso",
            issuer_url="https://idp.example.com/realms/spark",
            client_id="spark-dev-only",
            provider_preset="nonsense",
        )
        assert resp.status_code == 400


# -- Account security -----------------------------------------------------------------------


def _age_sessions(client: TestClient, user_id: uuid.UUID) -> None:
    """Make every session of the user older than warden's fresh-factor window."""

    async def age() -> None:
        async with _identity(client).db.transaction() as tx:
            await tx.execute(
                sql.SQL(
                    "UPDATE {} SET created_at = now() - interval '2 hours' WHERE user_id = %s"
                ).format(table(schema.SESSIONS)),
                (user_id,),
            )

    _call(client, age)


class TestAccountSecurity:
    def test_it_shows_the_users_own_method(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        assert sign_in(plain_client, ADA).status_code == 303
        page = _page(plain_client, "/account/mfa")
        assert re.search(r'id="own-method">Password \+ MFA', page.text)
        assert "The default for everyone" in page.text
        assert 'id="own-method-pending"' not in page.text

    def test_it_shows_a_change_waiting_for_the_user(self, admin: TestClient) -> None:
        ada = make_user(admin, ADA, USER)
        _post(admin, f"/admin/users/{ada}/sign-in-method", method="passkey_only")
        client = another_client(admin)
        assert sign_in(client, ADA).status_code == 303
        page = _page(client, "/account/mfa")
        assert 'id="own-method-pending"' in page.text
        assert "Register a passkey with the link emailed to you" in page.text
        assert "An administrator, for you" in page.text

    def test_codes_are_replaced_after_a_fresh_second_factor(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        assert sign_in(plain_client, ADA).status_code == 303
        first = _post(plain_client, "/account/mfa/recovery-codes")
        assert first.status_code == 200, first.text[:300]
        assert "Every earlier code no longer works" in first.text
        assert 'data-download="#recovery-codes"' in first.text
        assert "no-store" in first.headers["cache-control"]
        codes = re.search(r'<pre id="recovery-codes"[^>]*>(.*?)</pre>', first.text, re.S)
        assert codes and len(codes.group(1).split()) >= 8
        second = _post(plain_client, "/account/mfa/recovery-codes")
        again = re.search(r'<pre id="recovery-codes"[^>]*>(.*?)</pre>', second.text, re.S)
        assert again and set(again.group(1).split()).isdisjoint(codes.group(1).split())

    def test_a_stale_session_is_asked_for_a_fresh_factor_and_nothing_changes(
        self, plain_client: TestClient
    ) -> None:
        user = make_user(plain_client, ADA, USER)
        assert sign_in(plain_client, ADA).status_code == 303
        _age_sessions(plain_client, user)
        refused = _post(plain_client, "/account/mfa/recovery-codes")
        assert refused.status_code == 403
        assert message_of(refused) == FRESH_FACTOR_NEEDED
        assert 'id="recovery-codes"' not in refused.text
        assert 'id="fresh-factor-form"' in refused.text
        assert 'action="/auth/sign-out"' in refused.text

    def test_the_page_offers_the_button_with_a_second_factor(
        self, plain_client: TestClient
    ) -> None:
        make_user(plain_client, ADA, USER)
        assert sign_in(plain_client, ADA).status_code == 303
        assert 'action="/account/mfa/recovery-codes"' in _page(plain_client, "/account/mfa").text
