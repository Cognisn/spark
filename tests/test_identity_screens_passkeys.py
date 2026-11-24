"""Passkeys and security keys on SPARK's screens (server mode, warden 0.2.0).

The real app on a PostgreSQL clone at head with synthetic users; every
ceremony is answered by :class:`tests.webauthn_helpers.SoftAuthenticator`
(real keys and signatures, checked by warden as a browser's would be).

* Account security: register a passkey or security key with a name, list,
  rename and remove it; warden refuses removing the last second factor.
* The second factor: the challenge offers the key beside TOTP when warden
  names it, and forced enrolment offers it as warden names ``webauthn``.
* Passkey sign-in: offered only while warden can use passkeys; a
  ``passkey_only`` user signs in with a passkey alone and is refused a
  password sign-in with the generic message; another user is refused a
  passkey sign-in while passkeys are not an allowed second factor.
* Registration links and ``passkey_only`` invitations.
* The administrator's user page: the user's keys, a registration link and a
  reset of every second factor.

Every new route is checked for its permission and its CSRF token.
"""

from __future__ import annotations

import html
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.identity import SecondFactorKind, SignInMethod

from spark.identity.permissions import ADMINISTRATOR, USER
from spark.web.endpoints.account import LAST_FACTOR
from spark.web.endpoints.identity import (
    PASSKEY_SIGN_IN_FAILED,
    SIGN_IN_FAILED,
    VERIFICATION_FAILED,
)
from spark.web.security import CSRF_FORM_FIELD, CSRF_HEADER
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    ORIGIN,
    adopt_session,
    form_token,
    invite,
    message_of,
    mfa_client,
    plain_client,
    post_form,
    recovery_codes_on,
    server_db,
)
from tests.identity_web_helpers import (
    PASSWORD,
    PUBLIC,
    another_client,
    claims,
    cookie_header,
    make_user,
    password_step,
    runtime,
    sign_in,
)
from tests.webauthn_helpers import (
    SoftAuthenticator,
    as_field,
    form_action_with,
    options_from,
    options_url,
    public_key,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
LIN = "lin@example.com"
ACCESS = "__Host-spark-access"


def _call(client: TestClient, make: Any) -> Any:
    return client.portal.call(make)  # type: ignore[attr-defined]


def _allow(client: TestClient, *kinds: SecondFactorKind) -> None:
    identity = runtime(client).identity

    async def update() -> None:
        await identity.update_sign_in_settings(actor="test", allowed_second_factors=set(kinds))
        identity.sign_in_methods.invalidate()  # the next sign-in reads them at once

    _call(client, update)


def _keys(client: TestClient, user_id: uuid.UUID) -> list[Any]:
    identity = runtime(client).identity

    async def listed() -> list[Any]:
        return await identity.webauthn_credentials.list(user_id)

    result: list[Any] = _call(client, listed)
    return result


def _page(client: TestClient, path: str) -> Any:
    page = client.get(path, headers=NAVIGATE, follow_redirects=False)
    assert page.status_code == 200, (path, page.status_code, page.text[:300])
    return page


def _register_on_account(
    client: TestClient, key: SoftAuthenticator, nickname: str = "Laptop"
) -> Any:
    page = _page(client, "/account/mfa")
    options = public_key(options_from(client, options_url(page, "register"), page))
    return post_form(
        client,
        form_action_with(page, "register"),
        page,
        credential=as_field(key.create(options)),
        nickname=nickname,
    )


def _passkey_sign_in(client: TestClient, key: SoftAuthenticator, next_path: str = "/") -> Any:
    page = _page(client, "/auth/sign-in")
    options = public_key(options_from(client, options_url(page, "sign-in"), page))
    return post_form(
        client,
        form_action_with(page, "sign-in"),
        page,
        credential=as_field(key.get(options)),
        next=next_path,
    )


def _passkey_only_user(client: TestClient, email: str) -> uuid.UUID:
    identity = runtime(client).identity

    async def create() -> uuid.UUID:
        user = await identity.users.create(
            email, actor="test", sign_in_method=SignInMethod.PASSKEY_ONLY
        )
        await identity.roles.assign(user.id, USER, actor="test")
        return user.id

    result: uuid.UUID = _call(client, create)
    return result


def _admin(client: TestClient) -> TestClient:
    make_user(client, GRACE, ADMINISTRATOR)
    assert sign_in(client, GRACE).status_code == 303
    return client


@pytest.fixture
def ada(plain_client: TestClient) -> TestClient:
    make_user(plain_client, ADA, USER)
    assert sign_in(plain_client, ADA).status_code == 303
    return plain_client


# -- Account security ----------------------------------------------------------------------


class TestAccountSecurity:
    def test_the_page_offers_a_passkey_and_lists_none(self, ada: TestClient) -> None:
        page = _page(ada, "/account/mfa")
        assert 'data-webauthn="register"' in page.text
        assert 'src="/static/js/webauthn.js"' in page.text
        assert "No passkeys or security keys" in page.text
        assert 'href="/account/mfa"' in _page(ada, "/account").text

    def test_register_with_a_name_then_list_it(self, ada: TestClient) -> None:
        key = SoftAuthenticator()
        done = _register_on_account(ada, key, "Work laptop")
        # Ada already holds TOTP, so warden issues no new codes: back to the list.
        assert done.status_code == 303, done.text
        assert done.headers["location"] == "/account/mfa?done=passkey-added"
        page = _page(ada, done.headers["location"])
        assert "Work laptop" in page.text and "Passkey" in page.text

    def test_a_security_key_is_listed_as_one(self, ada: TestClient) -> None:
        _register_on_account(ada, SoftAuthenticator(resident=False, verifies_user=False), "Key")
        page = _page(ada, "/account/mfa")
        assert "Security key" in page.text

    def test_rename_and_remove(self, ada: TestClient) -> None:
        _register_on_account(ada, SoftAuthenticator(), "Old name")
        user_id = uuid.UUID(claims(ada.cookies[ACCESS])["sub"])
        (stored,) = _keys(ada, user_id)
        page = _page(ada, "/account/mfa")
        renamed = post_form(ada, f"/account/passkeys/{stored.id}/rename", page, nickname="New name")
        assert renamed.status_code == 303
        assert _keys(ada, user_id)[0].nickname == "New name"
        removed = post_form(ada, f"/account/passkeys/{stored.id}/remove", page)
        assert removed.status_code == 303
        assert _keys(ada, user_id) == []

    def test_an_invalid_name_is_refused(self, ada: TestClient) -> None:
        _register_on_account(ada, SoftAuthenticator())
        user_id = uuid.UUID(claims(ada.cookies[ACCESS])["sub"])
        (stored,) = _keys(ada, user_id)
        page = _page(ada, "/account/mfa")
        refused = post_form(ada, f"/account/passkeys/{stored.id}/rename", page, nickname="x" * 65)
        assert refused.status_code == 400
        assert _keys(ada, user_id)[0].nickname == "Laptop"

    def test_another_users_key_is_not_found(self, ada: TestClient) -> None:
        other = another_client(ada)
        make_user(other, LIN, USER)
        assert sign_in(other, LIN).status_code == 303
        _register_on_account(other, SoftAuthenticator())
        lin_id = uuid.UUID(claims(other.cookies[ACCESS])["sub"])
        (stored,) = _keys(other, lin_id)
        page = _page(ada, "/account/mfa")
        for action in ("rename", "remove"):
            resp = post_form(ada, f"/account/passkeys/{stored.id}/{action}", page, nickname="z")
            assert resp.status_code == 404, action
        assert _keys(other, lin_id)[0].nickname == "Laptop"

    def test_a_refused_answer_registers_nothing(self, ada: TestClient) -> None:
        page = _page(ada, "/account/mfa")
        options = public_key(options_from(ada, options_url(page, "register"), page))
        answer = SoftAuthenticator(origin="https://evil.example.net").create(options)
        refused = post_form(ada, "/account/passkeys", page, credential=as_field(answer))
        assert refused.status_code == 400
        assert "could not be registered" in message_of(refused)

    def test_the_routes_need_a_session(self, plain_client: TestClient) -> None:
        for path in (
            "/account/passkeys/options",
            "/account/passkeys",
            f"/account/passkeys/{uuid.uuid4()}/rename",
            f"/account/passkeys/{uuid.uuid4()}/remove",
        ):
            resp = plain_client.post(path, headers=ORIGIN, follow_redirects=False)
            assert resp.status_code in (401, 403), (path, resp.status_code)
        page = plain_client.get("/account/mfa", headers=NAVIGATE, follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"].startswith("/auth/sign-in")

    def test_the_routes_need_the_csrf_token(self, ada: TestClient) -> None:
        user_id = uuid.UUID(claims(ada.cookies[ACCESS])["sub"])
        _register_on_account(ada, SoftAuthenticator())
        (stored,) = _keys(ada, user_id)
        del ada.headers[CSRF_HEADER]
        for path in (
            "/account/passkeys/options",
            "/account/passkeys",
            f"/account/passkeys/{stored.id}/rename",
            f"/account/passkeys/{stored.id}/remove",
        ):
            resp = ada.post(path, data={"nickname": "x"}, follow_redirects=False)
            assert resp.status_code == 403, (path, resp.status_code)
        assert _keys(ada, user_id)[0].nickname == "Laptop"

    def test_an_api_key_cannot_register(self, ada: TestClient) -> None:
        identity = runtime(ada).identity
        user_id = uuid.UUID(claims(ada.cookies[ACCESS])["sub"])

        async def mint() -> str:
            created = await identity.api_keys.create(
                user_id, "k", frozenset({"spark.api_keys"}), actor="test"
            )
            return str(created.key)

        bearer = _call(ada, mint)
        bare = TestClient(ada.app, base_url=PUBLIC)
        bare.portal = ada.portal  # type: ignore[attr-defined]
        resp = bare.post("/account/passkeys/options", headers={"Authorization": f"Bearer {bearer}"})
        assert resp.status_code == 403


class TestLastFactor:
    def test_a_passkey_enrolled_at_sign_in_is_the_last_factor(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA, USER)
        key = SoftAuthenticator()
        done = _enrol_under_challenge(mfa_client, key)
        assert recovery_codes_on(done)
        adopt_session(mfa_client)
        user_id = uuid.UUID(claims(mfa_client.cookies[ACCESS])["sub"])
        (stored,) = _keys(mfa_client, user_id)
        page = _page(mfa_client, "/account/mfa")
        refused = post_form(mfa_client, f"/account/passkeys/{stored.id}/remove", page)
        assert refused.status_code == 409
        assert message_of(refused) == LAST_FACTOR
        assert len(_keys(mfa_client, user_id)) == 1


# -- The second factor ---------------------------------------------------------------------


def _enrol_under_challenge(client: TestClient, key: SoftAuthenticator, email: str = ADA) -> Any:
    resp = password_step(client, email)
    assert resp.headers["location"].startswith("/auth/challenge")
    page = _page(client, "/auth/challenge")
    assert 'data-webauthn="enrol"' in page.text
    options = public_key(options_from(client, options_url(page, "enrol"), page))
    done = post_form(
        client,
        form_action_with(page, "enrol"),
        page,
        credential=as_field(key.create(options)),
        nickname="Phone",
    )
    assert done.status_code == 200, done.text
    assert cookie_header(done, ACCESS)
    return done


class TestSecondFactor:
    def test_forced_enrolment_offers_a_passkey_beside_totp(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA, USER)
        password_step(mfa_client, ADA)
        page = _page(mfa_client, "/auth/challenge")
        assert 'id="totp-secret"' in page.text
        assert 'data-webauthn="enrol"' in page.text
        assert 'src="/static/js/webauthn.js"' in page.text

    def test_enrolling_a_passkey_signs_in_and_shows_the_codes_once(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA, USER)
        done = _enrol_under_challenge(mfa_client, SoftAuthenticator())
        assert len(recovery_codes_on(done)) >= 8
        assert done.headers["cache-control"] == "no-store"
        assert claims(mfa_client.cookies[ACCESS])["amr"] == ["pwd", "hwk"]

    def test_only_passkeys_allowed_enrols_a_passkey(self, mfa_client: TestClient) -> None:
        _allow(mfa_client, SecondFactorKind.PASSKEY)
        make_user(mfa_client, ADA, USER)
        password_step(mfa_client, ADA)
        page = _page(mfa_client, "/auth/challenge")
        assert 'id="totp-secret"' not in page.text
        assert 'id="factor-notice"' not in page.text
        assert 'data-webauthn="enrol"' in page.text
        mfa_client.cookies.clear()
        _enrol_under_challenge(mfa_client, SoftAuthenticator())

    def test_the_challenge_offers_the_key_and_signs_in_with_it(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA, USER)
        key = SoftAuthenticator(resident=False, verifies_user=False)
        _enrol_under_challenge(mfa_client, key)
        mfa_client.cookies.clear()
        resp = password_step(mfa_client, ADA, next_path="/conversations")
        page = _page(mfa_client, resp.headers["location"])
        assert 'data-webauthn="verify"' in page.text
        # No TOTP is held, so only the key and a recovery code are offered.
        assert 'name="kind" value="totp"' not in page.text
        assert 'id="factor-notice"' not in page.text
        options = public_key(options_from(mfa_client, options_url(page, "verify"), page))
        done = post_form(
            mfa_client,
            form_action_with(page, "verify"),
            page,
            credential=as_field(key.get(options)),
            next="/conversations",
        )
        assert done.status_code == 303, done.text
        assert done.headers["location"] == "/conversations"
        assert claims(mfa_client.cookies[ACCESS])["amr"] == ["pwd", "hwk"]

    def test_a_wrong_answer_is_the_uniform_refusal(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA, USER)
        key = SoftAuthenticator()
        _enrol_under_challenge(mfa_client, key)
        mfa_client.cookies.clear()
        password_step(mfa_client, ADA)
        page = _page(mfa_client, "/auth/challenge")
        options = public_key(options_from(mfa_client, options_url(page, "verify"), page))
        answer = SoftAuthenticator(origin="https://evil.example.net", keys=key.keys).get(options)
        refused = post_form(
            mfa_client, "/auth/challenge/webauthn", page, credential=as_field(answer)
        )
        assert refused.status_code == 401
        assert message_of(refused) == VERIFICATION_FAILED
        assert ACCESS not in mfa_client.cookies

    def test_a_totp_user_is_not_offered_a_key(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        leave = another_client(plain_client)
        resp = password_step(leave, ADA)
        page = _page(leave, resp.headers["location"])
        assert 'data-webauthn="verify"' not in page.text
        refused = options_from(leave, "/auth/challenge/webauthn/options", page)
        assert refused.status_code == 401

    def test_the_challenge_routes_need_its_csrf_token(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA, USER)
        password_step(mfa_client, ADA)
        _page(mfa_client, "/auth/challenge")
        for path in ("/auth/challenge/webauthn/options", "/auth/challenge/webauthn"):
            resp = mfa_client.post(path, headers=ORIGIN, data={"credential": "{}"})
            assert resp.status_code == 403, path
            forged = mfa_client.post(
                path, headers={**ORIGIN, CSRF_HEADER: "not-the-token"}, data={"credential": "{}"}
            )
            assert forged.status_code == 403, path

    def test_without_a_challenge_the_routes_start_over(self, plain_client: TestClient) -> None:
        resp = plain_client.post("/auth/challenge/webauthn/options", headers=ORIGIN)
        assert resp.status_code == 403  # no challenge, so no token can match


# -- Signing in with a passkey -------------------------------------------------------------


class TestPasskeySignIn:
    def test_the_sign_in_page_offers_a_passkey(self, plain_client: TestClient) -> None:
        page = _page(plain_client, "/auth/sign-in")
        assert 'data-webauthn="sign-in"' in page.text
        assert 'autocomplete="username webauthn"' in page.text
        assert "Sign in with a passkey" in page.text

    def test_not_offered_while_warden_cannot_use_passkeys(
        self, plain_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime(plain_client).identity, "webauthn", None)
        page = _page(plain_client, "/auth/sign-in")
        assert 'data-webauthn="sign-in"' not in page.text
        assert "webauthn.js" not in page.text

    def test_a_passkey_only_user_signs_in_with_a_passkey_alone(
        self, plain_client: TestClient
    ) -> None:
        admin = _admin(plain_client)
        lin_id = _passkey_only_user(admin, LIN)
        key = SoftAuthenticator()
        browser = another_client(admin)
        _register_by_link(admin, browser, lin_id, key)
        done = _passkey_sign_in(browser, key, next_path="/conversations")
        assert done.status_code == 303, done.text
        assert done.headers["location"] == "/conversations"
        assert claims(browser.cookies[ACCESS])["amr"] == ["hwk", "mfa"]
        assert claims(browser.cookies[ACCESS])["sub"] == str(lin_id)

    def test_a_passkey_only_user_is_refused_a_password_with_the_generic_message(
        self, plain_client: TestClient
    ) -> None:
        admin = _admin(plain_client)
        lin_id = _passkey_only_user(admin, LIN)
        browser = another_client(admin)
        _register_by_link(admin, browser, lin_id, SoftAuthenticator())
        refused = password_step(browser, LIN, PASSWORD)
        assert refused.status_code == 401
        assert message_of(refused) == SIGN_IN_FAILED

    def test_others_are_refused_while_passkeys_are_not_allowed(self, ada: TestClient) -> None:
        key = SoftAuthenticator()
        _register_on_account(ada, key)
        _allow(ada, SecondFactorKind.TOTP)
        browser = another_client(ada)
        refused = _passkey_sign_in(browser, key)
        assert refused.status_code == 401
        assert message_of(refused) == PASSKEY_SIGN_IN_FAILED
        assert ACCESS not in browser.cookies

    def test_others_sign_in_while_passkeys_are_allowed(self, ada: TestClient) -> None:
        key = SoftAuthenticator()
        _register_on_account(ada, key)
        browser = another_client(ada)
        done = _passkey_sign_in(browser, key)
        assert done.status_code == 303, done.text
        assert claims(browser.cookies[ACCESS])["amr"] == ["hwk", "mfa"]

    def test_a_security_key_is_not_a_passkey(self, ada: TestClient) -> None:
        key = SoftAuthenticator(resident=False, verifies_user=False)
        _register_on_account(ada, key)
        browser = another_client(ada)
        page = _page(browser, "/auth/sign-in")
        options = public_key(options_from(browser, options_url(page, "sign-in"), page))
        key.keys[-1].resident = True  # answer as if discoverable: warden still refuses it
        refused = post_form(
            browser, "/auth/sign-in/passkey", page, credential=as_field(key.get(options))
        )
        assert refused.status_code == 401
        assert message_of(refused) == PASSKEY_SIGN_IN_FAILED

    def test_a_malformed_answer_is_the_generic_refusal(self, plain_client: TestClient) -> None:
        page = _page(plain_client, "/auth/sign-in")
        refused = post_form(plain_client, "/auth/sign-in/passkey", page, credential="not json")
        assert refused.status_code == 401
        assert message_of(refused) == PASSKEY_SIGN_IN_FAILED

    def test_the_routes_need_the_sign_in_form_token(self, plain_client: TestClient) -> None:
        _page(plain_client, "/auth/sign-in")
        for path in ("/auth/sign-in/passkey/options", "/auth/sign-in/passkey"):
            assert plain_client.post(path, headers=ORIGIN).status_code == 403, path
            forged = plain_client.post(path, headers={**ORIGIN, CSRF_HEADER: "forged"})
            assert forged.status_code == 403, path
            other_origin = plain_client.post(
                path,
                headers={"Origin": "https://evil.example.net"},
                data={CSRF_FORM_FIELD: plain_client.cookies.get("__Secure-spark-sign-in", "")},
            )
            assert other_origin.status_code == 403, path


# -- Registration links and invitations ----------------------------------------------------


def _mailed_link(page: Any) -> str:
    """The link shown once to the administrator (no mail service here), as a path."""
    match = re.search(r'<code id="mailed-link"[^>]*>([^<]+)</code>', page.text)
    assert match, page.text
    link = html.unescape(match.group(1))
    assert link.startswith(f"{PUBLIC}/auth/passkey-registration?token=")
    return link[len(PUBLIC) :]


def _send_link(admin: TestClient, user_id: uuid.UUID) -> str:
    """An administrator sends a registration link; the link, as a path."""
    user_page = _page(admin, f"/admin/users/{user_id}")
    sent = post_form(admin, f"/admin/users/{user_id}/passkey-link", user_page)
    assert sent.status_code == 200, sent.text
    assert sent.headers["cache-control"] == "no-store"
    return _mailed_link(sent)


def _register_by_link(
    admin: TestClient,
    browser: TestClient,
    user_id: uuid.UUID,
    key: SoftAuthenticator,
    link: str | None = None,
) -> Any:
    page = _page(browser, link or _send_link(admin, user_id))
    assert 'data-webauthn="link"' in page.text
    assert page.headers["referrer-policy"] == "no-referrer"
    token = re.search(r'name="token" value="([^"]+)"', page.text)
    assert token
    options = public_key(
        options_from(browser, options_url(page, "link"), page, {"token": token.group(1)})
    )
    done = post_form(
        browser,
        form_action_with(page, "link"),
        page,
        token=token.group(1),
        credential=as_field(key.create(options)),
        nickname="Phone",
    )
    assert done.status_code == 200, done.text
    assert "Your passkey is registered" in done.text
    assert ACCESS not in browser.cookies  # registering signs nobody in
    return done


class TestRegistrationLink:
    def test_a_link_registers_a_passkey_and_shows_the_codes(self, plain_client: TestClient) -> None:
        admin = _admin(plain_client)
        lin_id = _passkey_only_user(admin, LIN)
        done = _register_by_link(admin, another_client(admin), lin_id, SoftAuthenticator())
        assert recovery_codes_on(done)
        assert done.headers["cache-control"] == "no-store"
        assert len(_keys(admin, lin_id)) == 1

    def test_a_used_link_is_not_valid(self, plain_client: TestClient) -> None:
        admin = _admin(plain_client)
        lin_id = _passkey_only_user(admin, LIN)
        browser = another_client(admin)
        link = _send_link(admin, lin_id)
        _register_by_link(admin, browser, lin_id, SoftAuthenticator(), link)
        page = _page(browser, link)
        token = link.split("token=", 1)[1]
        refused = options_from(
            browser, "/auth/passkey-registration/options", page, {"token": token}
        )
        assert refused.status_code == 401

    def test_the_options_need_the_page_token(self, plain_client: TestClient) -> None:
        _page(plain_client, "/auth/passkey-registration?token=x")
        resp = plain_client.post(
            "/auth/passkey-registration/options", json={"token": "x"}, headers=ORIGIN
        )
        assert resp.status_code == 403
        posted = plain_client.post(
            "/auth/passkey-registration", data={"token": "x"}, headers=ORIGIN
        )
        assert posted.status_code == 403


class TestPasskeyInvitation:
    def test_a_passkey_invitation_is_accepted_with_a_passkey(
        self, plain_client: TestClient
    ) -> None:
        user_id, link = invite(plain_client, LIN, USER, method=SignInMethod.PASSKEY_ONLY)
        page = _page(plain_client, link)
        assert 'data-webauthn="invitation"' in page.text
        assert 'name="password"' not in page.text
        token = link.split("token=", 1)[1]
        key = SoftAuthenticator()
        options = public_key(
            options_from(plain_client, options_url(page, "invitation"), page, {"token": token})
        )
        done = post_form(
            plain_client,
            form_action_with(page, "invitation"),
            page,
            token=token,
            method="passkey",
            credential=as_field(key.create(options)),
        )
        assert done.status_code == 200, done.text
        assert recovery_codes_on(done)
        assert len(_keys(plain_client, user_id)) == 1
        signed_in = _passkey_sign_in(another_client(plain_client), key)
        assert signed_in.status_code == 303

    def test_a_password_invitation_offers_no_passkey(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, LIN, USER)
        page = _page(plain_client, link)
        assert 'data-webauthn="invitation"' not in page.text
        assert 'name="password"' in page.text

    def test_the_options_need_the_page_token(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, LIN, USER, method=SignInMethod.PASSKEY_ONLY)
        _page(plain_client, link)
        resp = plain_client.post(
            "/auth/invitation/passkey/options", json={"token": "x"}, headers=ORIGIN
        )
        assert resp.status_code == 403


# -- Administration ------------------------------------------------------------------------


class TestAdministration:
    def test_the_user_page_lists_the_users_keys(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        browser = another_client(plain_client)
        assert sign_in(browser, ADA).status_code == 303
        _register_on_account(browser, SoftAuthenticator(), "Ada's phone")
        admin = _admin(plain_client)
        ada_id = uuid.UUID(claims(browser.cookies[ACCESS])["sub"])
        page = _page(admin, f"/admin/users/{ada_id}")
        assert 'id="user-passkeys"' in page.text
        assert "Ada&#39;s phone" in page.text or "Ada's phone" in page.text

    def test_reset_every_second_factor_removes_the_keys(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        browser = another_client(plain_client)
        sign_in(browser, ADA)
        _register_on_account(browser, SoftAuthenticator())
        ada_id = uuid.UUID(claims(browser.cookies[ACCESS])["sub"])
        admin = _admin(plain_client)
        page = _page(admin, f"/admin/users/{ada_id}")
        done = post_form(admin, f"/admin/users/{ada_id}/mfa-reset", page)
        assert done.status_code == 303
        assert _keys(admin, ada_id) == []

    def test_the_invitation_form_offers_passkey_only(self, plain_client: TestClient) -> None:
        admin = _admin(plain_client)
        page = _page(admin, "/admin/invitations")
        assert '<option value="passkey_only">' in page.text

    def test_a_passkey_only_invitation_is_listed_as_one(self, plain_client: TestClient) -> None:
        admin = _admin(plain_client)
        invite(admin, LIN, USER, method=SignInMethod.PASSKEY_ONLY)
        page = _page(admin, "/admin/invitations")
        assert ">passkey<" in page.text

    def test_the_admin_routes_need_the_permission(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        sign_in(plain_client, ADA)
        target = uuid.UUID(claims(plain_client.cookies[ACCESS])["sub"])
        for path in (f"/admin/users/{target}/passkey-link", f"/admin/users/{target}/mfa-reset"):
            assert plain_client.post(path).status_code == 403, path

    def test_the_admin_routes_need_the_csrf_token(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA, USER)
        target = _passkey_only_user(plain_client, LIN)
        admin = _admin(plain_client)
        del admin.headers[CSRF_HEADER]
        for path in (f"/admin/users/{target}/passkey-link", f"/admin/users/{target}/mfa-reset"):
            assert admin.post(path).status_code == 403, path
