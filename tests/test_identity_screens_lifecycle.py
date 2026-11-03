"""Invitations, password reset and single sign-on screens in server mode.

The real app on a PostgreSQL clone at head, synthetic users and invitations
created through warden. Mail is captured by a test sender standing in for
SPARK's (which shows links to administrators and otherwise sends nothing).
Single sign-on runs against a fake replacing ``runtime.identity.sso`` (question
18's interim option); the live test against mock-oauth2-server is in
``test_identity_sso_live.py``. Guards under test:

* an invitation or reset link works once; a bad, used or expired one gets one
  neutral message; a password warden refuses leaves the link usable;
* asking for a reset answers the same whether or not the address has an
  account, and mails nothing for an unknown one;
* single sign-on's login binding goes only to its own paths, the handoff code
  is exchanged on the server with this browser's binding, and every failure
  shows warden's fixed message and sets no session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from warden.identity import (
    SSO_FAILURE_MESSAGE,
    AuthenticationRefused,
    LoginStart,
    SignInMethod,
    SsoError,
    TokenPair,
)
from warden.identity.sso.settings import SsoSettings

from spark.identity.mail import InstanceMail, LinkCapture
from spark.identity.permissions import POWER_USER
from spark.identity.web import (
    ACCESS_COOKIE,
    FORM_CSRF_COOKIE,
    SSO_BINDING_COOKIE,
    SSO_HANDOFF_COOKIE,
)
from spark.web.endpoints.lifecycle import LINK_NOT_VALID, RESET_REQUESTED
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
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
)
from tests.identity_web_helpers import (
    PASSWORD,
    another_client,
    attributes,
    cookie_header,
    make_user,
    runtime,
    set_cookie_headers,
    sign_in,
)

pytestmark = pytest.mark.postgres

ADA = "ada@example.com"
GRACE = "grace@example.com"
NEW_PASSWORD = "a-new-long-password-dev-only"


def _sets(resp: Any, name: str) -> bool:
    return any(h.startswith(f"{name}=") and "Max-Age=0" not in h for h in set_cookie_headers(resp))


def _clears(resp: Any, name: str) -> bool:
    return any(
        h.startswith(f"{name}=") and ("Max-Age=0" in h or "expires=Thu, 01 Jan 1970" in h)
        for h in set_cookie_headers(resp)
    )


# -- Invitations --------------------------------------------------------------------------


class TestInvitation:
    def test_the_page_keeps_the_token_out_of_urls_and_referrers(
        self, plain_client: TestClient
    ) -> None:
        _, link = invite(plain_client, ADA)
        page = plain_client.get(link)
        assert page.status_code == 200
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"
        token = link.split("token=")[1]
        assert field(page, "token") == token
        form_cookie = attributes(cookie_header(page, FORM_CSRF_COOKIE))
        assert form_cookie["path"] == "/auth/invitation"

    def test_accepting_signs_in_with_the_staged_roles(self, plain_client: TestClient) -> None:
        user_id, link = invite(plain_client, ADA, POWER_USER)
        page = plain_client.get(link)
        resp = post_form(
            plain_client,
            "/auth/invitation",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/"
        assert _sets(resp, ACCESS_COOKIE)
        adopt_session(plain_client)
        me = plain_client.get("/auth/session", headers={"Accept": "application/json"}).json()
        assert me["email"] == ADA and me["roles"] == [POWER_USER]

    def test_accepting_goes_on_to_the_enrolment_warden_requires(
        self, mfa_client: TestClient
    ) -> None:
        _, link = invite(mfa_client, ADA)
        page = mfa_client.get(link)
        resp = post_form(
            mfa_client,
            "/auth/invitation",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/auth/challenge"
        assert not _sets(resp, ACCESS_COOKIE)
        enrol = mfa_client.get("/auth/challenge")
        done = post_form(mfa_client, "/auth/challenge/enrol", enrol, code=code(secret_on(enrol)))
        assert done.status_code == 200 and len(recovery_codes_on(done)) == 10
        assert _sets(done, ACCESS_COOKIE)

    def test_a_link_works_once(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, ADA)
        page = plain_client.get(link)
        fields = {"token": field(page, "token"), "password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
        assert post_form(plain_client, "/auth/invitation", page, **fields).status_code == 303
        plain_client.cookies.clear()
        page = plain_client.get(link)
        again = post_form(plain_client, "/auth/invitation", page, **fields)
        assert again.status_code == 401
        assert message_of(again) == LINK_NOT_VALID
        assert not _sets(again, ACCESS_COOKIE)

    @pytest.mark.parametrize("token", ["", "not-a-token", "A" * 43])
    def test_a_bad_link_gets_the_neutral_message(
        self, plain_client: TestClient, token: str
    ) -> None:
        page = plain_client.get("/auth/invitation?token=from-a-link")
        resp = post_form(
            plain_client,
            "/auth/invitation",
            page,
            token=token,
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert resp.status_code == 401 and message_of(resp) == LINK_NOT_VALID

    def test_a_short_password_leaves_the_link_usable(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, ADA)
        page = plain_client.get(link)
        token = field(page, "token")
        short = post_form(
            plain_client, "/auth/invitation", page, token=token, password="short", confirm="short"
        )
        assert short.status_code == 400 and "12" in message_of(short)
        assert field(short, "token") == token
        ok = post_form(
            plain_client,
            "/auth/invitation",
            short,
            token=token,
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert ok.status_code == 303

    def test_passwords_that_differ_are_refused_first(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, ADA)
        page = plain_client.get(link)
        resp = post_form(
            plain_client,
            "/auth/invitation",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD + "x",
        )
        assert resp.status_code == 400 and "do not match" in message_of(resp)

    def test_an_sso_only_invitation_is_not_accepted_with_a_password(
        self, plain_client: TestClient
    ) -> None:
        _, link = invite(plain_client, ADA, method=SignInMethod.SSO_ONLY)
        page = plain_client.get(link)
        resp = post_form(
            plain_client,
            "/auth/invitation",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert resp.status_code == 409 and "single sign-on" in message_of(resp)

    def test_the_form_needs_its_token(self, plain_client: TestClient) -> None:
        _, link = invite(plain_client, ADA)
        page = plain_client.get(link)
        resp = plain_client.post(
            "/auth/invitation",
            data={"token": field(page, "token"), "password": NEW_PASSWORD},
            headers={"Origin": "https://spark.example.com"},
        )
        assert resp.status_code == 403


# -- Password reset -----------------------------------------------------------------------


class TestPasswordReset:
    def test_the_answer_is_the_same_for_any_address(
        self, outbox: list[Any], plain_client: TestClient
    ) -> None:
        make_user(plain_client, ADA)
        answers = []
        for email in (ADA, "nobody@example.com", "not an address"):
            page = plain_client.get("/auth/password-reset")
            resp = post_form(plain_client, "/auth/password-reset", page, email=email)
            assert resp.status_code == 200
            answers.append(resp.text.replace(form_token(resp), ""))
        assert answers[0] == answers[1] == answers[2]
        assert RESET_REQUESTED in answers[0]
        assert [m.to for m in outbox] == [ADA]

    def test_reset_end_to_end(self, outbox: list[Any], plain_client: TestClient) -> None:
        make_user(plain_client, ADA)
        sign_in(plain_client, ADA)
        old_session = plain_client.cookies.get("__Host-spark-access")
        other = another_client(plain_client)
        page = other.get("/auth/password-reset")
        post_form(other, "/auth/password-reset", page, email=ADA)
        link = link_from(outbox, "/auth/reset")
        page = other.get(link)
        assert page.status_code == 200 and page.headers["referrer-policy"] == "no-referrer"
        done = post_form(
            other,
            "/auth/reset",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert done.status_code == 303
        assert done.headers["location"] == "/auth/sign-in?notice=reset"
        # The link worked once.
        page = other.get(link)
        again = post_form(
            other,
            "/auth/reset",
            page,
            token=field(page, "token"),
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert again.status_code == 401 and message_of(again) == LINK_NOT_VALID
        # Every session ended; the old password no longer works, the new one does.
        assert old_session
        stale = plain_client.get("/conversations", headers=NAVIGATE, follow_redirects=False)
        assert stale.status_code == 303
        assert sign_in(other, ADA).status_code == 401
        assert sign_in(other, ADA, NEW_PASSWORD).status_code == 303

    def test_a_short_password_leaves_the_link_usable(
        self, outbox: list[Any], plain_client: TestClient
    ) -> None:
        make_user(plain_client, ADA)
        page = plain_client.get("/auth/password-reset")
        post_form(plain_client, "/auth/password-reset", page, email=ADA)
        page = plain_client.get(link_from(outbox, "/auth/reset"))
        token = field(page, "token")
        short = post_form(
            plain_client, "/auth/reset", page, token=token, password="short", confirm="short"
        )
        assert short.status_code == 400 and field(short, "token") == token
        ok = post_form(
            plain_client,
            "/auth/reset",
            short,
            token=token,
            password=NEW_PASSWORD,
            confirm=NEW_PASSWORD,
        )
        assert ok.status_code == 303

    def test_without_a_mail_service_nothing_leaves_the_server(
        self, plain_client: TestClient
    ) -> None:
        make_user(plain_client, ADA)
        sender = runtime(plain_client).identity.mailer._sender
        assert type(sender) is InstanceMail and type(sender.fallback) is LinkCapture
        page = plain_client.get("/auth/password-reset")
        resp = post_form(plain_client, "/auth/password-reset", page, email=ADA)
        assert resp.status_code == 200 and RESET_REQUESTED in resp.text
        assert "/auth/reset?token=" not in resp.text


# -- Single sign-on (fake provider) -------------------------------------------------------


@dataclass
class FakeSso:
    """Stands in for warden's ``SingleSignOn``: the browser leg, without a provider."""

    identity: Any
    email: str = ADA
    enabled: bool = True
    fail_callback: bool = False
    bindings: list[str] = dc_field(default_factory=list)
    invitations: list[object] = dc_field(default_factory=list)
    handoffs: dict[str, str] = dc_field(default_factory=dict)

    @property
    def settings(self) -> Any:
        fake = self

        class Store:
            async def current(self) -> SsoSettings:
                return SsoSettings(
                    enabled=fake.enabled,
                    issuer_url="https://idp.example.com",
                    client_id="spark",
                    scopes=("openid", "email"),
                    allowed_email_domains=(),
                    provider_preset="generic",
                )

        return Store()

    async def aclose(self) -> None:
        """warden closes its provider client at shutdown."""

    async def begin(self, *, source_ip: Any = None, invitation: object = None) -> LoginStart:
        if not self.enabled:
            raise SsoError("sso_disabled")
        binding = uuid.uuid4().hex
        self.bindings.append(binding)
        self.invitations.append(invitation)
        return LoginStart("https://idp.example.com/authorize?state=s1", binding, 600)

    async def complete(
        self,
        *,
        state: object,
        code: object,
        error: object,
        binding: object,
        source_ip: Any = None,
        invitations: Any = None,
    ) -> str:
        if self.fail_callback or binding not in self.bindings or state != "s1":
            raise SsoError("transaction_unknown")
        handoff = uuid.uuid4().hex
        self.handoffs[handoff] = str(binding)
        return handoff

    async def exchange(self, code: object, binding: object, *, source_ip: Any = None) -> TokenPair:
        if not isinstance(code, str) or self.handoffs.pop(code, None) != binding:
            raise AuthenticationRefused("handoff_unknown")
        result = await self.identity.sign_in(self.email, PASSWORD)
        assert result.tokens is not None
        tokens: TokenPair = result.tokens
        return tokens


@pytest.fixture
def sso(plain_client: TestClient) -> FakeSso:
    identity = runtime(plain_client).identity
    fake = FakeSso(identity)
    identity.sso = fake
    make_user(plain_client, ADA)
    return fake


def _through_provider(client: TestClient) -> Any:
    """Start, come back from the 'provider' and land on the completion screen."""
    start = client.get("/identity/auth/sso/start", follow_redirects=False)
    assert start.status_code == 302, start.text
    assert start.headers["location"].startswith("https://idp.example.com/authorize")
    back = client.get(
        "/identity/auth/sso/callback?state=s1&code=provider-code", follow_redirects=False
    )
    return back


class TestSingleSignOn:
    def test_the_sign_in_page_offers_it_when_enabled(
        self, plain_client: TestClient, sso: FakeSso
    ) -> None:
        assert 'href="/identity/auth/sso/start"' in plain_client.get("/auth/sign-in").text
        sso.enabled = False
        assert "/identity/auth/sso/start" not in plain_client.get("/auth/sign-in").text

    def test_start_binds_the_login_to_this_browser(
        self, plain_client: TestClient, sso: FakeSso
    ) -> None:
        start = plain_client.get("/identity/auth/sso/start", follow_redirects=False)
        assert start.status_code == 302
        assert start.headers["referrer-policy"] == "no-referrer"
        binding = attributes(cookie_header(start, SSO_BINDING_COOKIE))
        assert binding["path"] == "/identity/auth/sso"
        assert binding["samesite"].lower() == "lax"
        assert "secure" in binding and "httponly" in binding

    def test_completion_signs_in(self, plain_client: TestClient, sso: FakeSso) -> None:
        back = _through_provider(plain_client)
        assert back.status_code == 302
        assert back.headers["location"].startswith("/auth/sso/complete?code=")
        assert _clears(back, SSO_BINDING_COOKIE)
        handoff = attributes(cookie_header(back, SSO_HANDOFF_COOKIE))
        assert handoff["path"] == "/auth/sso/complete"
        done = plain_client.get(back.headers["location"], follow_redirects=False)
        assert done.status_code == 200
        assert done.headers["cache-control"] == "no-store"
        assert _sets(done, ACCESS_COOKIE) and _clears(done, SSO_HANDOFF_COOKIE)
        assert plain_client.get("/conversations", headers=NAVIGATE).status_code == 200

    def test_a_failure_shows_the_fixed_message(
        self, plain_client: TestClient, sso: FakeSso
    ) -> None:
        sso.fail_callback = True
        back = _through_provider(plain_client)
        assert back.headers["location"] == "/auth/sso/complete?error=sso_failed"
        page = plain_client.get(back.headers["location"])
        assert message_of(page) == SSO_FAILURE_MESSAGE
        assert not _sets(page, ACCESS_COOKIE)

    def test_disabled_lands_on_the_failure(self, plain_client: TestClient, sso: FakeSso) -> None:
        sso.enabled = False
        start = plain_client.get("/identity/auth/sso/start", follow_redirects=False)
        assert start.headers["location"] == "/auth/sso/complete?error=sso_failed"

    def test_a_handoff_code_from_another_browser_is_refused(
        self, plain_client: TestClient, sso: FakeSso
    ) -> None:
        back = _through_provider(plain_client)
        other = another_client(plain_client)
        page = other.get(back.headers["location"], follow_redirects=False)
        assert page.status_code == 401
        assert message_of(page) == SSO_FAILURE_MESSAGE
        assert not _sets(page, ACCESS_COOKIE)

    def test_an_invitation_can_be_accepted_through_it(
        self, plain_client: TestClient, sso: FakeSso
    ) -> None:
        _, link = invite(plain_client, GRACE, method=SignInMethod.SSO_ONLY)
        page = plain_client.get(link)
        assert 'name="method" value="sso"' in page.text
        token = field(page, "token")
        resp = post_form(plain_client, "/auth/invitation", page, token=token, method="sso")
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("https://idp.example.com/authorize")
        assert sso.invitations[-1] == token
        assert attributes(cookie_header(resp, SSO_BINDING_COOKIE))["path"] == "/identity/auth/sso"
