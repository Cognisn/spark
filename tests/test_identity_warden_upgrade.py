"""SPARK on warden 0.2.0: the upgrade from 0.3.0rc44, the always-on second factor and personal mode.

warden 0.2.0 (ruling CS26) asks for a second factor on every password sign-in,
sends a user holding no allowed factor to forced enrolment, and adds one
migration (``f1c7d24e8b93``). Guards under test:

* a database at 0.3.0rc44's schema (SPARK's chain at head, warden's at 0.1.x's
  head ``a9e4c27f13d8``) is refused until ``spark migrate``, which runs
  warden's revision; afterwards nobody's sign-in method has changed (CS16): a
  password user without a second factor is sent to enrol one, a TOTP user
  answers a code, and a single sign-on user still signs in through the
  provider with nothing more asked;
* a right password alone never reaches a session: it answers with the
  challenge screen, and no session cookie is set until the second factor;
* forced enrolment offers only the kinds warden names (TOTP while WebAuthn is
  not configured), and shows the recovery codes warden returns, once;
* personal mode is unchanged: no warden, no challenge screens, its terminal
  code signs in in one step.

The 0.3.0rc44 database is made by building users on warden's head and then
running warden's own tested downgrade to ``a9e4c27f13d8``, which restores the
0.1.x shape exactly (every user's ``sign_in_method`` set, ``password_and_sso``
for password users); the users' passwords, TOTP keys and recovery codes are
the rows 0.1.x keeps in tables 0.2.0 does not change. All synthetic.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import psycopg
import pytest
import warden.db
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from warden.identity import SignInMethod
from warden.identity.sso.provider import ProviderHttp

from spark.db import migrate
from spark.db.migrate import MigrationRequired
from spark.identity import migrations as identity_migrations
from spark.identity.web import ACCESS_COOKIE, CHALLENGE_COOKIE, REFRESH_COOKIE, csrf_token_for
from spark.mode import DatabaseSettings, Dialect
from spark.web.endpoints.identity import NO_ENROLMENT, SIGN_IN_FAILED
from spark.web.security import CSRF_FORM_FIELD
from spark.web.server import create_app
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    mfa_client,
    post_form,
    server_db,
)
from tests.identity_web_helpers import (
    PUBLIC,
    Factor,
    enrol_totp,
    leave_second_factor_to_test,
    make_user,
    password_step,
    recovery_codes_on,
    runtime,
    second_factor_answer,
    secret_on,
    server_client,
    set_cookie_headers,
)
from tests.identity_web_helpers import sign_in as warden_sign_in
from tests.identity_web_helpers import (
    write_config,
)
from tests.test_instance_settings_web import MemorySecrets
from tests.web_helpers import sign_in as personal_sign_in

# warden 0.1.x's head (0.1.0 and 0.1.1): where every 0.3.0rc44 database has warden.
RC44_WARDEN_HEAD = "a9e4c27f13d8"
WARDEN_HEAD = "f1c7d24e8b93"

ADA = "ada@example.com"  # a password, no second factor
GRACE = "grace@example.com"  # a password and TOTP
LIN = "lin@example.com"  # single sign-on only

ISSUER = "https://idp.example.com/realms/spark"
CLIENT_ID = "spark-dev-only"
SECRET = "synthetic-client-secret-dev-only"  # never a real client secret


def _sets(resp: Any, name: str) -> bool:
    return any(h.startswith(f"{name}=") and "Max-Age=0" not in h for h in set_cookie_headers(resp))


def _signed_in(client: TestClient) -> bool:
    return client.get("/auth/session", headers={"Accept": "application/json"}).status_code == 200


def _call(client: TestClient, fn: Any) -> Any:
    return client.portal.call(fn)  # type: ignore[union-attr]


# -- A synthetic OpenID Connect provider that signs ID tokens ---------------------------


class Provider:
    """idp.example.com behind an ``httpx.MockTransport``: discovery, keys and tokens."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce = ""
        self.subject = "lin-subject"
        self.email = LIN

    def jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        jwk = {"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256"}
        return {"keys": [{**jwk, "n": b64(numbers.n), "e": b64(numbers.e)}]}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": self.subject,
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "nonce": self.nonce,
            "email": self.email,
            "email_verified": True,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json=self.jwks())
        if url == f"{ISSUER}/token":
            return httpx.Response(
                200,
                json={"id_token": self.id_token(), "token_type": "Bearer", "access_token": "x"},
            )
        return httpx.Response(404, json={})


def _use_provider(client: TestClient, provider: Provider) -> None:
    sso = runtime(client).identity.sso
    sso._http = ProviderHttp(httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)))


def _set_up_sso(client: TestClient) -> None:
    sso = runtime(client).identity.sso

    async def configure() -> None:
        await sso.set_client_secret(SECRET, actor="test")
        await sso.settings.update(
            actor="test", enabled=True, issuer_url=ISSUER, client_id=CLIENT_ID
        )

    _call(client, configure)


def _sso_sign_in(client: TestClient, provider: Provider) -> Any:
    """Single sign-on through the real app and warden, the provider synthetic."""
    start = client.get("/identity/auth/sso/start", follow_redirects=False)
    assert start.status_code == 302, start.text
    authorize = urlsplit(start.headers["location"])
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == f"{ISSUER}/authorize"
    query = parse_qs(authorize.query)
    provider.nonce = query["nonce"][0]
    back = client.get(
        "/identity/auth/sso/callback",
        params={"state": query["state"][0], "code": "provider-code-dev-only"},
        follow_redirects=False,
    )
    assert back.status_code == 302, back.text
    assert back.headers["location"].startswith("/auth/sso/complete?code="), back.headers
    return client.get(back.headers["location"], follow_redirects=False)


# -- The upgrade from 0.3.0rc44 ----------------------------------------------------------


@pytest.fixture
def _isolated_chains() -> Iterator[None]:
    saved = migrate.registered_chains()
    migrate.clear_chains()
    identity_migrations.register()
    yield
    migrate.clear_chains()
    for chain in saved:
        migrate.register_chain(chain)


def _methods(url: str) -> dict[str, str | None]:
    with psycopg.connect(url) as conn:
        rows = conn.execute("SELECT email, sign_in_method FROM warden.users").fetchall()
    return {email: method for email, method in rows}


@pytest.mark.postgres
@pytest.mark.usefixtures("_isolated_chains")
class TestUpgradeFromRc44:
    def _rc44_database(
        self, tmp_path: Path, pg_url: str, secrets: MemorySecrets
    ) -> tuple[DatabaseSettings, Factor]:
        """A database at rc44's schema with Ada, Grace (TOTP) and Lin (single sign-on only).

        ``secrets`` is the server's secrets store (its TOTP, signing and CSRF keys
        and the client secret), kept for the upgraded server as a deployment would.
        """
        settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)
        migrate.migrate_all(settings)
        config = write_config(tmp_path / "build.yaml")
        with server_client(config, settings, secrets) as client:
            leave_second_factor_to_test(client)
            _set_up_sso(client)
            identity = runtime(client).identity

            async def build() -> Factor:
                await identity.users.create(ADA, actor="test", password="correct-horse-1-dev-only")
                await identity.users.create(
                    GRACE, actor="test", password="correct-horse-2-dev-only"
                )
                await identity.users.create(LIN, actor="test", sign_in_method=SignInMethod.SSO_ONLY)
                return await enrol_totp(identity, GRACE, "correct-horse-2-dev-only")

            factor: Factor = _call(client, build)
        warden.db.downgrade(pg_url, RC44_WARDEN_HEAD)
        assert warden.db.current_revision(pg_url) == RC44_WARDEN_HEAD
        # 0.1.x's shape: every user's method is set; nothing pending, no instance default.
        assert _methods(pg_url) == {
            ADA: "password_and_sso",
            GRACE: "password_and_sso",
            LIN: "sso_only",
        }
        return settings, factor

    def test_spark_migrate_runs_warden_0_2_0_and_changes_nobody(
        self, tmp_path: Path, pg_url: str
    ) -> None:
        secrets = MemorySecrets()
        settings, _ = self._rc44_database(tmp_path, pg_url, secrets)
        with pytest.raises(MigrationRequired, match=RC44_WARDEN_HEAD):
            migrate.check_at_head(settings)
        assert migrate.migrate_all(settings) == {"spark": migrate.head(), "warden": WARDEN_HEAD}
        migrate.check_at_head(settings)
        # CS16: password users follow the default, itself "password or single sign-on";
        # the single sign-on user keeps it as their own setting.
        assert _methods(pg_url) == {ADA: None, GRACE: None, LIN: "sso_only"}
        with psycopg.connect(pg_url) as conn:
            default = conn.execute(
                "SELECT default_sign_in_method FROM warden.sign_in_settings"
            ).fetchone()
            pending = conn.execute("SELECT count(*) FROM warden.sign_in_method_changes").fetchone()
        assert default == ("password_and_sso",) and pending == (0,)

        with server_client(write_config(tmp_path / "config.yaml"), settings, secrets) as client:
            identity = runtime(client).identity

            async def in_force() -> dict[str, tuple[Any, Any]]:
                found = {}
                for email in (ADA, GRACE, LIN):
                    user = await identity.users.get_by_email(email)
                    assert user is not None
                    found[email] = (user.sign_in_method, user.own_sign_in_method)
                return found

            assert _call(client, in_force) == {
                ADA: (SignInMethod.PASSWORD_AND_SSO, None),
                GRACE: (SignInMethod.PASSWORD_AND_SSO, None),
                LIN: (SignInMethod.SSO_ONLY, SignInMethod.SSO_ONLY),
            }

    def test_each_user_signs_in_as_before_apart_from_the_second_factor(
        self, tmp_path: Path, pg_url: str
    ) -> None:
        secrets = MemorySecrets()
        settings, grace_factor = self._rc44_database(tmp_path, pg_url, secrets)
        migrate.migrate_all(settings)
        provider = Provider()
        with server_client(write_config(tmp_path / "config.yaml"), settings, secrets) as client:
            leave_second_factor_to_test(client)
            _use_provider(client, provider)

            # Ada (a password alone before): the password, then forced enrolment of TOTP.
            resp = password_step(client, ADA, "correct-horse-1-dev-only")
            assert resp.status_code == 303 and resp.headers["location"] == "/auth/challenge"
            assert not _sets(resp, ACCESS_COOKIE) and not _signed_in(client)
            page = client.get("/auth/challenge")
            secret = secret_on(page)
            done = post_form(
                client, "/auth/challenge/enrol", page, code=second_factor_answer(Factor(secret))[1]
            )
            assert done.status_code == 200, done.text
            assert len(recovery_codes_on(done)) == 10
            assert _sets(done, ACCESS_COOKIE) and _signed_in(client)
            client.cookies.clear()

            # Grace (TOTP before): the password, then her code, as before.
            resp = password_step(client, GRACE, "correct-horse-2-dev-only")
            assert resp.headers["location"] == "/auth/challenge"
            page = client.get("/auth/challenge")
            assert 'id="totp-secret"' not in page.text
            kind, value = second_factor_answer(grace_factor)
            assert kind == "totp"
            done = post_form(client, "/auth/challenge/verify", page, code=value, kind=kind)
            assert done.status_code == 303 and done.headers["location"] == "/"
            assert _sets(done, ACCESS_COOKIE) and _signed_in(client)
            client.cookies.clear()

            # Lin (single sign-on only): no password, and nothing asked after the provider.
            refused = password_step(client, LIN, "correct-horse-1-dev-only")
            assert refused.status_code == 401 and SIGN_IN_FAILED in refused.text
            done = _sso_sign_in(client, provider)
            assert done.status_code == 200, done.text
            assert _sets(done, ACCESS_COOKIE) and _signed_in(client)

            async def lin_method() -> Any:
                user = await runtime(client).identity.users.get_by_email(LIN)
                return user.sign_in_method if user is not None else None

            assert _call(client, lin_method) is SignInMethod.SSO_ONLY


# -- A password alone never reaches a session --------------------------------------------


@pytest.mark.postgres
class TestPasswordAlone:
    def test_a_right_password_without_a_factor_goes_to_enrolment_not_a_session(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA)
        resp = password_step(mfa_client, ADA, next_path="/conversations")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/challenge?next=%2Fconversations"
        assert not _sets(resp, ACCESS_COOKIE) and not _sets(resp, REFRESH_COOKIE)
        # warden names TOTP and, with the public address as relying party, security keys;
        # this screen enrols TOTP only.
        assert (mfa_client.cookies.get(CHALLENGE_COOKIE) or "").startswith("enrol:totp+webauthn:")
        assert not _signed_in(mfa_client)
        page = mfa_client.get("/conversations", headers=NAVIGATE, follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"].startswith("/auth/sign-in")

    def test_a_right_password_with_a_factor_goes_to_the_code_not_a_session(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA)
        _call(mfa_client, lambda: enrol_totp(runtime(mfa_client).identity, ADA))
        resp = password_step(mfa_client, ADA)
        assert resp.status_code == 303 and resp.headers["location"] == "/auth/challenge"
        assert not _sets(resp, ACCESS_COOKIE)
        assert (mfa_client.cookies.get(CHALLENGE_COOKIE) or "").startswith("verify:totp:")
        assert not _signed_in(mfa_client)
        page = mfa_client.get("/auth/challenge")
        assert 'action="/auth/challenge/verify"' in page.text
        assert 'id="totp-secret"' not in page.text

    def test_the_helpers_sign_in_answers_the_factor(self, mfa_client: TestClient) -> None:
        # The shared helper reaches a session only by answering the challenge.
        client = TestClient(mfa_client.app, base_url=PUBLIC)
        client.portal = mfa_client.portal
        make_user(client, GRACE)
        resp = warden_sign_in(client, GRACE)
        assert resp.status_code == 303 and resp.headers["location"] == "/"
        assert _signed_in(client)


# -- Forced enrolment shows only the kinds warden names ---------------------------------


@pytest.mark.postgres
class TestForcedEnrolment:
    def test_without_a_kind_named_nothing_is_offered_and_nothing_signs_in(
        self, mfa_client: TestClient
    ) -> None:
        make_user(mfa_client, ADA)
        password_step(mfa_client, ADA)
        purpose, kinds, token = (mfa_client.cookies.get(CHALLENGE_COOKIE) or "").split(":")
        assert (purpose, kinds) == ("enrol", "totp+webauthn")
        # As if warden had named no kind this page can enrol. (Security keys and
        # passkeys alone are enrolled here since wave 26: test_identity_screens_passkeys.)
        mfa_client.cookies.set(
            CHALLENGE_COOKIE,
            f"enrol::{token}",
            domain="spark.example.com",
            path="/auth/challenge",
        )
        page = mfa_client.get("/auth/challenge")
        assert page.status_code == 200
        assert NO_ENROLMENT in page.text
        assert 'id="totp-secret"' not in page.text
        assert 'action="/auth/challenge/enrol"' not in page.text
        assert 'action="/auth/challenge/webauthn"' not in page.text
        # A forged confirmation, with the challenge's own form token, starts the user over.
        csrf = csrf_token_for(runtime(mfa_client).csrf_key, "challenge", f"enrol::{token}")
        confirm = mfa_client.post(
            "/auth/challenge/enrol",
            data={CSRF_FORM_FIELD: csrf, "code": "123456"},
            headers={"Origin": PUBLIC},
            follow_redirects=False,
        )
        assert confirm.status_code == 303 and confirm.headers["location"] == "/auth/sign-in"
        assert not _signed_in(mfa_client)

    def test_a_challenge_begun_before_the_upgrade_still_takes_totp(
        self, mfa_client: TestClient
    ) -> None:
        # rc46 kept "verify:<token>" (TOTP was the only kind); a sign-in in flight keeps TOTP.
        make_user(mfa_client, ADA)
        factor = _call(mfa_client, lambda: enrol_totp(runtime(mfa_client).identity, ADA))
        password_step(mfa_client, ADA)
        token = (mfa_client.cookies.get(CHALLENGE_COOKIE) or "").rpartition(":")[2]
        mfa_client.cookies.set(
            CHALLENGE_COOKIE, f"verify:{token}", domain="spark.example.com", path="/auth/challenge"
        )
        page = mfa_client.get("/auth/challenge")
        assert 'id="factor-notice"' not in page.text
        kind, value = second_factor_answer(factor)
        done = post_form(mfa_client, "/auth/challenge/verify", page, code=value, kind=kind)
        assert done.status_code == 303 and _sets(done, ACCESS_COOKIE)

    def test_codes_warden_does_not_return_are_not_shown(
        self, mfa_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A user replacing a factor no longer allowed keeps their codes: warden returns none.
        identity = runtime(mfa_client).identity
        original = identity.confirm_enrolment

        async def without_codes(*args: Any, **kwargs: Any) -> Any:
            _, outcome = await original(*args, **kwargs)
            return [], outcome

        monkeypatch.setattr(identity, "confirm_enrolment", without_codes)
        make_user(mfa_client, ADA)
        password_step(mfa_client, ADA)
        page = mfa_client.get("/auth/challenge")
        code = second_factor_answer(Factor(secret_on(page)))[1]
        done = post_form(mfa_client, "/auth/challenge/enrol", page, code=code)
        assert done.status_code == 303 and done.headers["location"] == "/"
        assert 'id="recovery-codes"' not in done.text
        assert _sets(done, ACCESS_COOKIE) and _signed_in(mfa_client)


# -- Personal mode is unchanged ----------------------------------------------------------


def _personal_ctx() -> MagicMock:
    values: dict[str, Any] = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
        "interface.ssl.enabled": False,
    }

    def get(key: str, default: object = None, *, cast: type | None = None) -> object:
        value = values.get(key, default)
        return cast(value) if cast is not None and value is not None else value

    ctx = MagicMock()
    ctx.settings.get = get
    return ctx


class TestPersonalMode:
    def test_the_terminal_code_signs_in_in_one_step_with_no_challenge(self) -> None:
        client = TestClient(create_app(_personal_ctx(), first_run=False))
        # Signed out, everything goes to the personal sign-in page, never a challenge.
        for path in ("/auth/sign-in", "/auth/challenge"):
            resp = client.get(path, headers=NAVIGATE, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
        cookies = personal_sign_in(client)  # the terminal code: one step, no second factor
        assert cookies
        assert client.get("/", follow_redirects=False).status_code == 200
        for path in ("/auth/sign-in", "/auth/challenge"):
            assert client.get(path, follow_redirects=False).status_code == 404
        assert getattr(client.app.state, "identity_runtime", None) is None
