"""The sign-in screens in server mode: password, second factor, enrolment and recovery codes.

Every test runs the real app on a PostgreSQL clone at head with synthetic users
created through warden. Guards under test:

* a challenge is answered only from the browser holding its cookie: its forms'
  CSRF token is bound to the challenge, and a challenge named in a form is
  ignored;
* every refused code gets one message, whichever factor was tried;
* the enrolment warden requires shows a QR code and the key, signs in on
  confirmation and shows the recovery codes once, never cached;
* ``next`` is followed only to a same-origin path.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from spark.identity.web import ACCESS_COOKIE, CHALLENGE_COOKIE, REFRESH_COOKIE
from spark.web.endpoints.identity import VERIFICATION_FAILED
from spark.web.security import CSRF_FORM_FIELD
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    ORIGIN,
    adopt_session,
    code,
    enrol_under_challenge,
    field,
    form_token,
    message_of,
    mfa_client,
    plain_client,
    post_form,
    recovery_codes_on,
    secret_on,
    server_db,
    verify,
)
from tests.identity_web_helpers import (
    another_client,
    make_user,
    set_cookie_headers,
    sign_in,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"


def _sets(resp: Any, name: str) -> bool:
    return any(h.startswith(f"{name}=") and "Max-Age=0" not in h for h in set_cookie_headers(resp))


def _clears(resp: Any, name: str) -> bool:
    return any(
        h.startswith(f"{name}=") and ("Max-Age=0" in h or "expires=Thu, 01 Jan 1970" in h)
        for h in set_cookie_headers(resp)
    )


def _enrolled(client: TestClient, email: str) -> tuple[bytes, list[str]]:
    """A user enrolled under the policy's challenge, then signed out again."""
    make_user(client, email)
    secret, codes, _ = enrol_under_challenge(client, email)
    client.cookies.clear()
    return secret, codes


class TestSignInPage:
    def test_renders_with_its_form_token_and_links(self, plain_client: TestClient) -> None:
        page = plain_client.get("/auth/sign-in")
        assert page.status_code == 200
        assert form_token(page)
        assert 'href="/auth/password-reset"' in page.text
        assert "single sign-on" not in page.text  # not configured
        assert page.headers["cache-control"] == "no-store"

    def test_shows_only_fixed_notices(self, plain_client: TestClient) -> None:
        page = plain_client.get("/auth/sign-in?notice=reset")
        assert "Your password is changed" in page.text
        page = plain_client.get("/auth/sign-in?notice=%3Cscript%3Ealert(1)%3C/script%3E")
        assert "alert(1)" not in page.text

    def test_a_password_without_a_second_factor_signs_in(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA)
        resp = sign_in(plain_client, ADA)
        assert resp.status_code == 303 and resp.headers["location"] == "/"
        assert _sets(resp, ACCESS_COOKIE)


class TestEnrolmentUnderChallenge:
    def test_the_enrolment_page_shows_a_qr_code_and_the_key(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        page = mfa_client.get("/auth/challenge")
        assert page.status_code == 200
        assert 'src="data:image/svg+xml;base64,' in page.text
        assert len(secret_on(page)) == 20
        assert "otpauth://totp/" in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"

    def test_confirming_signs_in_and_shows_the_recovery_codes_once(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA)
        secret, codes, done = enrol_under_challenge(mfa_client, ADA)
        assert len(codes) == 10 and all(len(c) == 11 for c in codes)
        assert done.headers["cache-control"] == "no-store"
        assert done.headers["referrer-policy"] == "no-referrer"
        assert _sets(done, ACCESS_COOKIE) and _sets(done, REFRESH_COOKIE)
        assert _clears(done, CHALLENGE_COOKIE)
        # Signed in: pages load.
        assert mfa_client.get("/conversations", headers=NAVIGATE).status_code == 200
        # A second visit shows no codes: the challenge is gone, and replaying is refused.
        again = mfa_client.get("/auth/challenge", follow_redirects=False)
        assert again.status_code == 303 and "recovery-codes" not in again.text

    def test_a_wrong_code_keeps_the_key_on_screen_with_the_uniform_message(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        page = mfa_client.get("/auth/challenge")
        secret = secret_on(page)
        wrong = post_form(
            mfa_client,
            "/auth/challenge/enrol",
            page,
            code="000000" if code(secret) != "000000" else "111111",
            secret=field(page, "secret"),
            uri=field(page, "uri"),
        )
        assert wrong.status_code == 401
        assert message_of(wrong) == VERIFICATION_FAILED
        assert secret_on(wrong) == secret
        assert not _sets(wrong, ACCESS_COOKIE)
        # The same challenge still enrols with the right code.
        done = post_form(mfa_client, "/auth/challenge/enrol", wrong, code=code(secret))
        assert done.status_code == 200
        assert len(recovery_codes_on(done)) == 10

    def test_a_posted_back_key_that_is_not_a_key_is_not_shown(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        page = mfa_client.get("/auth/challenge")
        wrong = post_form(
            mfa_client,
            "/auth/challenge/enrol",
            page,
            code="000000",
            secret="<b>not a key</b>",
            uri="https://evil.example.net/",
        )
        assert wrong.status_code == 401
        assert "not a key" not in wrong.text and "evil.example.net" not in wrong.text


class TestVerify:
    def test_a_totp_code_signs_in_and_goes_to_next(self, mfa_client: TestClient) -> None:
        secret, _ = _enrolled(mfa_client, ADA)
        resp = sign_in(mfa_client, ADA)
        assert resp.headers["location"].startswith("/auth/challenge")
        page = mfa_client.get("/auth/challenge")
        assert 'name="kind" value="recovery"' in page.text
        done = post_form(
            mfa_client,
            "/auth/challenge/verify",
            page,
            code=code(secret, 1),
            kind="totp",
            next="/conversations",
        )
        assert done.status_code == 303 and done.headers["location"] == "/conversations"
        assert _sets(done, ACCESS_COOKIE) and _clears(done, CHALLENGE_COOKIE)

    @pytest.mark.parametrize(
        "target", ["https://evil.example.net/", "//evil.example.net/", "/\\evil.example.net"]
    )
    def test_next_off_this_origin_goes_home(self, mfa_client: TestClient, target: str) -> None:
        secret, _ = _enrolled(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        done = verify(mfa_client, code(secret, 1), next_path=target)
        assert done.status_code == 303 and done.headers["location"] == "/"

    def test_a_recovery_code_signs_in_once(self, mfa_client: TestClient) -> None:
        _, codes = _enrolled(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        done = verify(mfa_client, codes[0].upper(), kind="recovery")
        assert done.status_code == 303 and _sets(done, ACCESS_COOKIE)
        mfa_client.cookies.clear()
        sign_in(mfa_client, ADA)
        again = verify(mfa_client, codes[0], kind="recovery")
        assert again.status_code == 401 and not _sets(again, ACCESS_COOKIE)

    def test_every_refused_factor_gets_the_same_message(self, mfa_client: TestClient) -> None:
        secret, codes = _enrolled(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        wrong_totp = verify(mfa_client, "000000" if code(secret) != "000000" else "111111")
        wrong_recovery = verify(mfa_client, "aaaaa-aaaaa", kind="recovery")
        malformed = verify(mfa_client, "not a code")
        for resp in (wrong_totp, wrong_recovery, malformed):
            assert resp.status_code == 401
            assert message_of(resp) == VERIFICATION_FAILED
            assert not _sets(resp, ACCESS_COOKIE)

    def test_without_a_challenge_the_screens_go_back_to_sign_in(
        self, mfa_client: TestClient
    ) -> None:
        resp = mfa_client.get("/auth/challenge", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/auth/sign-in"


class TestChallengeBinding:
    def test_a_form_from_another_browsers_challenge_is_refused(
        self, mfa_client: TestClient
    ) -> None:
        secret, _ = _enrolled(mfa_client, ADA)
        make_user(mfa_client, GRACE)
        other = another_client(mfa_client)
        sign_in(mfa_client, ADA)
        ada_page = mfa_client.get("/auth/challenge")
        sign_in(other, GRACE)
        grace_page = other.get("/auth/challenge")
        assert form_token(ada_page) != form_token(grace_page)
        # Grace's browser posting a form from Ada's challenge page.
        resp = other.post(
            "/auth/challenge/enrol",
            data={CSRF_FORM_FIELD: form_token(ada_page), "code": code(secret, 1)},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert not _sets(resp, ACCESS_COOKIE)

    def test_a_challenge_named_in_the_form_is_ignored(self, mfa_client: TestClient) -> None:
        secret, _ = _enrolled(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        ada_challenge = mfa_client.cookies.get(CHALLENGE_COOKIE) or ""
        make_user(mfa_client, GRACE)
        other = another_client(mfa_client)
        sign_in(other, GRACE)
        grace_page = other.get("/auth/challenge")
        resp = post_form(
            other,
            "/auth/challenge/verify",
            grace_page,
            code=code(secret, 1),
            challenge=ada_challenge.partition(":")[2],
        )
        assert resp.status_code == 303  # Grace's enrol challenge does not verify
        assert not _sets(resp, ACCESS_COOKIE)
