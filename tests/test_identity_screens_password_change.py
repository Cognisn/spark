"""warden 0.1.0's password policies on SPARK's screens (server mode).

warden may require a new password at sign-in, after the password and any
second factor are right and before any session exists
(``PasswordChangeIssued``). SPARK keeps that forced-change challenge in the
same challenge cookie as a second factor (``change:<token>``, scoped to
``/auth/challenge``) and asks for the new password there; only then are the
session cookies set. A password warden refuses anywhere shows warden's plain
messages, never its codes. Guards under test:

* a required change after a password, a TOTP code, a recovery code or a
  forced enrolment sets no session cookie, only the change challenge;
* the change form is answered only from the browser holding that challenge,
  and only a change challenge (not a verify or enrol one) is accepted there;
* a refused new password keeps the challenge and shows the messages; the
  accepted one signs in, goes to ``next`` and ends the challenge;
* the user's own change, an invitation and a reset show the messages for a
  refused password instead of failing.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.identity import ChangeReason

from spark.identity import passwords
from spark.identity.web import ACCESS_COOKIE, CHALLENGE_COOKIE, REFRESH_COOKIE
from spark.web.security import CSRF_FORM_FIELD
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    ORIGIN,
    adopt_session,
    code,
    field,
    form_token,
    invite,
    link_from,
    message_of,
    mfa_client,
    outbox,
    plain_client,
    post_form,
    recovery_codes_on,
    secret_on,
    server_db,
    verify,
)
from tests.identity_web_helpers import (
    PASSWORD,
    another_client,
    make_user,
    runtime,
    set_cookie_headers,
    sign_in,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
NEW_PASSWORD = "a-fresh-long-password-dev-only"
# On warden's built-in list of common passwords, and long enough for the default policy.
COMMON = "qwertyqwerty"
CHANGE_PATH = "/auth/challenge/change"


def _sets(resp: Any, name: str) -> bool:
    return any(h.startswith(f"{name}=") and "Max-Age=0" not in h for h in set_cookie_headers(resp))


def _require_change(client: TestClient, user_id: uuid.UUID) -> None:
    """An administrator requires ``user_id`` to change their password (warden's API)."""
    identity = runtime(client).identity

    async def flag() -> None:
        await identity.password_states.require_change(user_id, ChangeReason.ADMINISTRATOR)

    client.portal.call(flag)  # type: ignore[attr-defined]


def _challenge_purpose(client: TestClient) -> str:
    return (client.cookies.get(CHALLENGE_COOKIE) or "").partition(":")[0]


def _change(client: TestClient, password: str, confirm: str | None = None, **extra: str) -> Any:
    page = client.get("/auth/challenge")
    assert page.status_code == 200, page.text
    return post_form(
        client,
        CHANGE_PATH,
        page,
        password=password,
        confirm=password if confirm is None else confirm,
        **extra,
    )


class TestRequiredAtSignIn:
    def test_a_required_change_asks_for_a_password_and_sets_no_session(
        self, plain_client: TestClient
    ) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        resp = sign_in(plain_client, ADA)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/auth/challenge")
        assert not _sets(resp, ACCESS_COOKIE) and not _sets(resp, REFRESH_COOKIE)
        assert _challenge_purpose(plain_client) == "change"
        page = plain_client.get(resp.headers["location"])
        assert page.status_code == 200
        assert f'action="{CHANGE_PATH}"' in page.text
        assert passwords.CHANGE_REASONS[ChangeReason.ADMINISTRATOR] in page.text
        assert page.headers["cache-control"] == "no-store"
        # Still not signed in.
        held = plain_client.get("/conversations", headers=NAVIGATE, follow_redirects=False)
        assert held.status_code == 303

    def test_the_new_password_signs_in_and_goes_to_next(self, plain_client: TestClient) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        sign_in(plain_client, ADA)
        done = _change(plain_client, NEW_PASSWORD, next="/conversations")
        assert done.status_code == 303 and done.headers["location"] == "/conversations"
        assert _sets(done, ACCESS_COOKIE) and _sets(done, REFRESH_COOKIE)
        assert not _sets(done, CHALLENGE_COOKIE)
        assert plain_client.get("/conversations", headers=NAVIGATE).status_code == 200
        # The old password no longer signs in; the new one does, with no change asked.
        other = another_client(plain_client)
        assert sign_in(other, ADA).status_code == 401
        again = sign_in(other, ADA, NEW_PASSWORD)
        assert again.status_code == 303 and again.headers["location"] == "/"

    def test_a_refused_password_shows_the_messages_and_keeps_the_challenge(
        self, plain_client: TestClient
    ) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        sign_in(plain_client, ADA)
        refused = _change(plain_client, COMMON)
        assert refused.status_code == 400
        assert "too common" in message_of(refused)
        assert "watch_list" not in refused.text
        assert not _sets(refused, ACCESS_COOKIE)
        assert _challenge_purpose(plain_client) == "change"
        done = post_form(
            plain_client, CHANGE_PATH, refused, password=NEW_PASSWORD, confirm=NEW_PASSWORD
        )
        assert done.status_code == 303 and _sets(done, ACCESS_COOKIE)

    def test_passwords_that_differ_are_refused_first(self, plain_client: TestClient) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        sign_in(plain_client, ADA)
        differ = _change(plain_client, NEW_PASSWORD, NEW_PASSWORD + "x")
        assert differ.status_code == 400
        assert message_of(differ) == passwords.PASSWORDS_DIFFER
        assert not _sets(differ, ACCESS_COOKIE)

    def test_next_off_this_origin_goes_home(self, plain_client: TestClient) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        sign_in(plain_client, ADA)
        done = _change(plain_client, NEW_PASSWORD, next="https://evil.example.net/")
        assert done.status_code == 303 and done.headers["location"] == "/"

    def test_a_used_challenge_starts_again(self, plain_client: TestClient) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        sign_in(plain_client, ADA)
        page = plain_client.get("/auth/challenge")
        challenge = plain_client.cookies.get(CHALLENGE_COOKIE) or ""
        assert (
            post_form(
                plain_client, CHANGE_PATH, page, password=NEW_PASSWORD, confirm=NEW_PASSWORD
            ).status_code
            == 303
        )
        # Replaying the spent challenge from another browser is refused.
        other = another_client(plain_client)
        other.cookies.set(CHALLENGE_COOKIE, challenge, path="/auth/challenge")
        replay_page = other.get("/auth/challenge")
        replay = post_form(
            other, CHANGE_PATH, replay_page, password=NEW_PASSWORD + "2", confirm=NEW_PASSWORD + "2"
        )
        assert replay.status_code == 303
        assert replay.headers["location"].startswith("/auth/sign-in")
        assert not _sets(replay, ACCESS_COOKIE)


class TestChallengeBinding:
    def test_a_form_from_another_browsers_challenge_is_refused(
        self, plain_client: TestClient
    ) -> None:
        _require_change(plain_client, make_user(plain_client, ADA))
        _require_change(plain_client, make_user(plain_client, GRACE))
        other = another_client(plain_client)
        sign_in(plain_client, ADA)
        ada_page = plain_client.get("/auth/challenge")
        sign_in(other, GRACE)
        resp = other.post(
            CHANGE_PATH,
            data={
                CSRF_FORM_FIELD: form_token(ada_page),
                "password": NEW_PASSWORD,
                "confirm": NEW_PASSWORD,
            },
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert not _sets(resp, ACCESS_COOKIE)

    def test_a_verify_challenge_cannot_change_the_password(self, mfa_client: TestClient) -> None:
        make_user(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        assert _challenge_purpose(mfa_client) == "enrol"
        page = mfa_client.get("/auth/challenge")
        resp = post_form(mfa_client, CHANGE_PATH, page, password=NEW_PASSWORD, confirm=NEW_PASSWORD)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/auth/sign-in")
        assert not _sets(resp, ACCESS_COOKIE)
        # The password is unchanged.
        mfa_client.cookies.clear()
        assert sign_in(mfa_client, ADA).status_code == 303
        assert _challenge_purpose(mfa_client) == "enrol"


class TestAfterASecondFactor:
    def test_a_totp_code_then_the_change(self, mfa_client: TestClient) -> None:
        user_id = make_user(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        page = mfa_client.get("/auth/challenge")
        secret = secret_on(page)
        assert (
            post_form(mfa_client, "/auth/challenge/enrol", page, code=code(secret)).status_code
            == 200
        )
        mfa_client.cookies.clear()
        _require_change(mfa_client, user_id)
        sign_in(mfa_client, ADA)
        resp = verify(mfa_client, code(secret, 1), next_path="/conversations")
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/auth/challenge")
        assert not _sets(resp, ACCESS_COOKIE)
        assert _challenge_purpose(mfa_client) == "change"
        done = _change(mfa_client, NEW_PASSWORD, next="/conversations")
        assert done.status_code == 303 and done.headers["location"] == "/conversations"
        assert _sets(done, ACCESS_COOKIE)

    def test_a_recovery_code_then_the_change(self, mfa_client: TestClient) -> None:
        user_id = make_user(mfa_client, ADA)
        sign_in(mfa_client, ADA)
        page = mfa_client.get("/auth/challenge")
        done = post_form(mfa_client, "/auth/challenge/enrol", page, code=code(secret_on(page)))
        codes = recovery_codes_on(done)
        mfa_client.cookies.clear()
        _require_change(mfa_client, user_id)
        sign_in(mfa_client, ADA)
        resp = verify(mfa_client, codes[0], kind="recovery")
        assert resp.status_code == 303 and not _sets(resp, ACCESS_COOKIE)
        assert _challenge_purpose(mfa_client) == "change"
        assert _change(mfa_client, NEW_PASSWORD).status_code == 303

    def test_a_forced_enrolment_shows_the_codes_then_asks_for_the_password(
        self, mfa_client: TestClient
    ) -> None:
        _require_change(mfa_client, make_user(mfa_client, ADA))
        sign_in(mfa_client, ADA, PASSWORD)
        page = mfa_client.get("/auth/challenge")
        done = post_form(
            mfa_client, "/auth/challenge/enrol", page, code=code(secret_on(page)), next="/files"
        )
        assert done.status_code == 200
        assert len(recovery_codes_on(done)) == 10
        assert done.headers["cache-control"] == "no-store"
        assert not _sets(done, ACCESS_COOKIE) and not _sets(done, REFRESH_COOKIE)
        assert _challenge_purpose(mfa_client) == "change"
        assert "you are signed in" not in done.text
        assert 'href="/auth/challenge?' in done.text
        finished = _change(mfa_client, NEW_PASSWORD, next="/files")
        assert finished.status_code == 303 and finished.headers["location"] == "/files"
        assert _sets(finished, ACCESS_COOKIE)


class TestRefusedPasswordsElsewhere:
    def test_the_users_own_change_shows_the_messages(self, plain_client: TestClient) -> None:
        make_user(plain_client, ADA)
        assert sign_in(plain_client, ADA).status_code == 303
        resp = plain_client.post(
            "/account/password",
            data={"current": PASSWORD, "password": COMMON, "confirm": COMMON},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "too common" in message_of(resp)
        assert sign_in(another_client(plain_client), ADA).status_code == 303

    def test_an_invitation_shows_the_messages(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, ADA)
        page = plain_client.get(link)
        resp = post_form(
            plain_client,
            "/auth/invitation",
            page,
            token=field(page, "token"),
            password=COMMON,
            confirm=COMMON,
        )
        assert resp.status_code == 400
        assert message_of(resp) == "This password is too common."

    def test_a_reset_shows_the_messages(self, outbox: list[Any], plain_client: TestClient) -> None:
        make_user(plain_client, ADA)
        identity = runtime(plain_client).identity

        async def ask() -> None:
            await identity.resets.request(ADA)

        plain_client.portal.call(ask)  # type: ignore[attr-defined]
        page = plain_client.get(link_from(outbox, "/auth/reset"))
        resp = post_form(
            plain_client,
            "/auth/reset",
            page,
            token=field(page, "token"),
            password=COMMON,
            confirm=COMMON,
        )
        assert resp.status_code == 400
        assert message_of(resp) == "This password is too common."
