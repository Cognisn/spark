"""Single sign-on administration and its wording in server mode (``spark.users.admin``).

The real app on a PostgreSQL clone at head with synthetic users; the OpenID
Connect provider is synthetic too (an ``httpx.MockTransport`` answering for
``idp.example.com``), so nothing leaves the test. Guards under test:

* ``/admin/sso`` and its actions need ``spark.users.admin``: other roles get
  403 and change nothing, a signed-out page load goes to sign-in;
* the client secret goes to the secrets store, is never shown, returned or
  audited by value, and a blank field keeps the stored one;
* saving writes warden's settings, audited by warden with names only;
  switching on needs a stored secret;
* the Test action reports warden's checks against the provider (good, wrong
  issuer, unreachable) and signs nobody in;
* every SSO button, wording and sign-in method choice is hidden until single
  sign-on is switched on and configured, and shown once it is;
* SSO-only users are flagged while single sign-on is off and can be moved to
  a password; switching it off while any exist asks first and names them.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from warden.audit.query import AuditQuery, list_events
from warden.identity import SignInMethod
from warden.identity.sso.provider import ProviderHttp

from spark.identity import sso as sso_settings
from spark.identity.permissions import ADMINISTRATOR, AUDITOR, POWER_USER, USER
from spark.web.endpoints.administration import SSO_OFF
from tests.identity_screen_helpers import (  # noqa: F401 - fixtures
    NAVIGATE,
    invite,
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
ISSUER = "https://idp.example.com/realms/spark"
CLIENT_ID = "spark-dev-only"
# Synthetic: never a real client secret.
SECRET = "synthetic-client-secret-dev-only"
SECRET_NAME = "warden.sso.client-secret"
SSO_PATH = "/admin/sso"
CALLBACK = f"{PUBLIC}/identity/auth/sso/callback"


# -- A synthetic OpenID Connect provider ------------------------------------------------


def _discovery(issuer: str) -> dict[str, Any]:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["S256"],
    }


# A public key's shape only: the configuration test reads key ids and types, not key material.
_JWKS = {
    "keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256", "n": "AQAB", "e": "AQAB"}]
}


class Provider:
    """Answers for the synthetic issuer; ``mode`` is good, wrong-issuer or unreachable."""

    def __init__(self) -> None:
        self.mode = "good"
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self.mode == "unreachable":
            raise httpx.ConnectError("synthetic: no route", request=request)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            issuer = ISSUER if self.mode == "good" else "https://other.example.com/realms/x"
            return httpx.Response(200, json=_discovery(issuer))
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json=_JWKS)
        return httpx.Response(404, json={})


@pytest.fixture
def provider(plain_client: TestClient) -> Provider:
    fake = Provider()
    sso = runtime(plain_client).identity.sso
    # warden builds its provider client on first use; this one goes nowhere.
    sso._http = ProviderHttp(httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    return fake


# -- Helpers ----------------------------------------------------------------------------


@pytest.fixture
def admin(plain_client: TestClient) -> TestClient:
    make_user(plain_client, ADMIN, ADMINISTRATOR)
    assert sign_in(plain_client, ADMIN).status_code == 303
    return plain_client


def _identity(client: TestClient) -> Any:
    return runtime(client).identity


def _call(client: TestClient, fn: Callable[[], Any]) -> Any:
    return client.portal.call(fn)  # type: ignore[attr-defined]


def _secrets(client: TestClient) -> Any:
    return _identity(client).sso._secrets


def _page(client: TestClient, path: str) -> Any:
    page = client.get(path, headers=NAVIGATE, follow_redirects=False)
    assert page.status_code == 200, (path, page.status_code, page.text[:300])
    return page


def _post(client: TestClient, path: str, **fields: Any) -> Any:
    return client.post(path, data=fields, follow_redirects=False)


def _save(client: TestClient, **fields: Any) -> Any:
    data = {"issuer_url": ISSUER, "client_id": CLIENT_ID, "client_secret": "", **fields}
    return _post(client, SSO_PATH, **data)


def _enable(client: TestClient) -> None:
    resp = _save(client, client_secret=SECRET, enabled="on")
    assert resp.status_code == 303, message_of(resp) or resp.text[:300]


def _settings(client: TestClient) -> Any:
    async def read() -> Any:
        return await sso_settings.state(_identity(client))

    return _call(client, read)


def _events(client: TestClient, *actions: str) -> list[Any]:
    async def read() -> list[Any]:
        page = await list_events(_identity(client).db, AuditQuery(actions=actions))
        return list(page.rows)

    result: list[Any] = _call(client, read)
    return result


def _make(client: TestClient, email: str, method: SignInMethod) -> uuid.UUID:
    async def create() -> uuid.UUID:
        user = await _identity(client).users.create(email, actor="test", sign_in_method=method)
        await _identity(client).roles.assign(user.id, USER, actor="test")
        return user.id

    result: uuid.UUID = _call(client, create)
    return result


def _method_options(page: Any) -> list[str]:
    """The sign-in method values a page's create, invite or change form offers."""
    return re.findall(r'<option value="(password_and_sso|sso_only)"', page.text)


# -- Access -----------------------------------------------------------------------------


class TestAccess:
    @pytest.mark.parametrize("role", [USER, POWER_USER, AUDITOR])
    def test_other_roles_are_refused(self, plain_client: TestClient, role: str) -> None:
        make_user(plain_client, ADA, role)
        sign_in(plain_client, ADA)
        assert plain_client.get(SSO_PATH, headers=NAVIGATE).status_code == 403
        assert _save(plain_client, client_secret=SECRET, enabled="on").status_code == 403
        assert _post(plain_client, SSO_PATH + "/test").status_code == 403
        state = _settings(plain_client)
        assert not state.settings.enabled and state.settings.issuer_url is None
        assert not state.secret_set

    def test_signed_out_goes_to_sign_in(self, plain_client: TestClient) -> None:
        resp = plain_client.get(SSO_PATH, headers=NAVIGATE, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/auth/sign-in")

    def test_an_administrator_reaches_it_from_the_users_page(self, admin: TestClient) -> None:
        assert f'href="{SSO_PATH}"' in _page(admin, "/admin/users").text
        page = _page(admin, SSO_PATH)
        assert "Single sign-on" in page.text
        assert CALLBACK in page.text  # the redirect URI to register, for copying
        assert page.headers["cache-control"] == "no-store"


# -- Settings and the client secret -----------------------------------------------------


class TestSettings:
    def test_saving_writes_warden_settings_and_stores_the_secret(self, admin: TestClient) -> None:
        resp = _save(admin, client_secret=SECRET, enabled="on")
        assert resp.status_code == 303 and resp.headers["location"] == f"{SSO_PATH}?done=saved"
        state = _settings(admin)
        assert state.settings.enabled
        assert state.settings.issuer_url == ISSUER and state.settings.client_id == CLIENT_ID
        assert _secrets(admin).get(SECRET_NAME) == SECRET
        assert state.available

    def test_the_secret_is_never_shown_or_returned(self, admin: TestClient) -> None:
        saved = _save(admin, client_secret=SECRET, enabled="on")
        assert SECRET not in saved.text
        page = _page(admin, SSO_PATH + "?done=saved")
        assert SECRET not in page.text
        assert re.search(r'<input[^>]*name="client_secret"[^>]*>', page.text)
        field = re.search(r'<input[^>]*name="client_secret"[^>]*>', page.text)
        assert field and "value=" not in field.group(0)
        assert "A client secret is stored" in page.text
        # A refused save does not echo it either.
        refused = _save(admin, issuer_url="http://idp.example.com", client_secret=SECRET + "-2")
        assert refused.status_code == 400 and SECRET not in refused.text

    def test_a_blank_secret_keeps_the_stored_one_and_a_new_one_replaces_it(
        self, admin: TestClient
    ) -> None:
        _enable(admin)
        assert _save(admin, enabled="on").status_code == 303
        assert _secrets(admin).get(SECRET_NAME) == SECRET
        assert _save(admin, client_secret=SECRET + "-new", enabled="on").status_code == 303
        assert _secrets(admin).get(SECRET_NAME) == SECRET + "-new"

    def test_switching_on_needs_a_secret(self, admin: TestClient) -> None:
        resp = _save(admin, enabled="on")
        assert resp.status_code == 400
        assert message_of(resp) == sso_settings.SECRET_REQUIRED
        assert not _settings(admin).settings.enabled

    def test_an_http_issuer_is_refused_by_name(self, admin: TestClient) -> None:
        resp = _save(admin, issuer_url="http://idp.example.com", client_secret=SECRET)
        assert resp.status_code == 400 and "issuer URL" in message_of(resp)
        assert "http://idp.example.com" not in message_of(resp)
        assert _settings(admin).settings.issuer_url is None

    def test_saving_is_audited_by_name_only(self, admin: TestClient) -> None:
        _enable(admin)
        changed = _events(admin, "warden.identity.sso.settings_changed")
        assert changed and changed[0].outcome == "success"
        assert sorted(changed[0].details["changed"]) == ["client_id", "enabled", "issuer_url"]
        stored = _events(admin, "warden.identity.sso.client_secret_set")
        assert stored and stored[0].details == {"secret_name": SECRET_NAME}
        everything = json.dumps([e.details for e in changed + stored], default=str)
        assert SECRET not in everything and ISSUER not in everything


# -- The Test action --------------------------------------------------------------------


class TestCheck:
    def test_a_good_provider_passes(self, admin: TestClient, provider: Provider) -> None:
        _enable(admin)
        page = _post(admin, SSO_PATH + "/test")
        assert page.status_code == 200
        assert "All checks passed" in page.text
        assert page.text.count("Passed.") == len(sso_settings.CHECKS)
        assert provider.requests[0] == f"{ISSUER}/.well-known/openid-configuration"
        assert not any("/token" in url or "/authorize" in url for url in provider.requests)
        tested = _events(admin, "warden.identity.sso.configuration_tested")
        assert tested and tested[0].outcome == "success"

    def test_a_wrong_issuer_is_reported(self, admin: TestClient, provider: Provider) -> None:
        _enable(admin)
        provider.mode = "wrong-issuer"
        page = _post(admin, SSO_PATH + "/test")
        assert page.status_code == 200
        assert sso_settings.REASONS["issuer_mismatch"] in html.unescape(page.text)
        assert "All checks passed" not in page.text

    def test_an_unreachable_provider_is_reported(
        self, admin: TestClient, provider: Provider
    ) -> None:
        _enable(admin)
        provider.mode = "unreachable"
        page = _post(admin, SSO_PATH + "/test")
        assert page.status_code == 200
        assert sso_settings.REASONS["discovery_unavailable"] in html.unescape(page.text)
        tested = _events(admin, "warden.identity.sso.configuration_tested")
        assert tested and tested[0].outcome == "failure"

    def test_nothing_configured_is_reported_without_reaching_out(
        self, admin: TestClient, provider: Provider
    ) -> None:
        page = _post(admin, SSO_PATH + "/test")
        assert page.status_code == 200
        assert sso_settings.REASONS["missing"] in html.unescape(page.text)
        assert provider.requests == []


# -- Wording follows whether single sign-on is available --------------------------------


def _invitation_page(client: TestClient, email: str) -> Any:
    _, link = invite(client, email)
    return another_client(client).get(link)


class TestWording:
    def test_hidden_until_configured_and_switched_on(self, admin: TestClient) -> None:
        sign_in_page = another_client(admin).get("/auth/sign-in")
        assert "/identity/auth/sso/start" not in sign_in_page.text
        assert "single sign-on" not in sign_in_page.text
        invitation = _invitation_page(admin, GRACE)
        assert 'value="sso"' not in invitation.text and "single sign-on" not in invitation.text
        account = _page(admin, "/account")
        assert re.search(r"Sign-in</span>\s*<span>Password</span>", account.text)
        assert "single sign-on" not in account.text
        users = _page(admin, "/admin/users")
        assert _method_options(users) == ["password_and_sso"]
        assert "A password, or single sign-on" not in users.text
        assert "password or SSO" not in users.text
        user = _page(admin, f"/admin/users/{make_user(admin, ADA)}")
        assert "single sign-on" not in user.text and "sso_only" not in user.text

    def test_hidden_while_configured_but_switched_off(self, admin: TestClient) -> None:
        assert _save(admin, client_secret=SECRET).status_code == 303
        assert not _settings(admin).available
        assert "/identity/auth/sso/start" not in another_client(admin).get("/auth/sign-in").text
        assert _method_options(_page(admin, "/admin/users")) == ["password_and_sso"]

    def test_hidden_while_switched_on_without_a_secret(self, admin: TestClient) -> None:
        async def switch_on() -> None:
            await _identity(admin).sso.settings.update(
                actor="test", enabled=True, issuer_url=ISSUER, client_id=CLIENT_ID
            )

        _call(admin, switch_on)
        assert "/identity/auth/sso/start" not in another_client(admin).get("/auth/sign-in").text
        assert _method_options(_page(admin, "/admin/users")) == ["password_and_sso"]

    def test_shown_once_switched_on(self, admin: TestClient) -> None:
        _enable(admin)
        assert 'href="/identity/auth/sso/start"' in another_client(admin).get("/auth/sign-in").text
        invitation = _invitation_page(admin, GRACE)
        assert 'value="sso"' in invitation.text
        assert "accept through single sign-on" in invitation.text
        account = _page(admin, "/account")
        assert "Password or single sign-on" in account.text
        users = _page(admin, "/admin/users")
        assert _method_options(users) == ["password_and_sso", "sso_only"]
        assert "A password, or single sign-on" in users.text

    def test_an_sso_only_user_cannot_be_created_while_it_is_off(self, admin: TestClient) -> None:
        resp = _post(admin, "/admin/users", email=ADA, method="sso_only")
        assert resp.status_code == 400 and message_of(resp) == SSO_OFF
        refused = _post(admin, "/admin/invitations", email=ADA, method="sso_only")
        assert refused.status_code == 400

    def test_an_sso_only_user_can_be_created_once_it_is_on(self, admin: TestClient) -> None:
        _enable(admin)
        resp = _post(admin, "/admin/users", email=ADA, method="sso_only")
        assert resp.status_code == 303


# -- SSO-only users while single sign-on is off -----------------------------------------


class TestSsoOnlyUsers:
    def test_flagged_and_changed_to_a_password(self, admin: TestClient) -> None:
        ada_id = _make(admin, ADA, SignInMethod.SSO_ONLY)
        users = _page(admin, "/admin/users")
        assert "Cannot sign in: single sign-on is off" in users.text
        user = _page(admin, f"/admin/users/{ada_id}")
        assert "Cannot sign in: single sign-on is off" in user.text
        assert f'action="/admin/users/{ada_id}/sign-in-method"' in user.text
        resp = _post(admin, f"/admin/users/{ada_id}/sign-in-method", method="password_and_sso")
        assert resp.status_code == 303

        async def method() -> SignInMethod:
            found = await _identity(admin).users.get(ada_id)
            assert found is not None
            result: SignInMethod = found.sign_in_method
            return result

        assert _call(admin, method) is SignInMethod.PASSWORD_AND_SSO
        assert "Cannot sign in" not in _page(admin, "/admin/users").text

    def test_not_moved_to_sso_only_while_it_is_off(self, admin: TestClient) -> None:
        ada_id = make_user(admin, ADA)
        resp = _post(admin, f"/admin/users/{ada_id}/sign-in-method", method="sso_only")
        assert resp.status_code == 400

    def test_not_flagged_while_it_is_on(self, admin: TestClient) -> None:
        _enable(admin)
        _make(admin, ADA, SignInMethod.SSO_ONLY)
        assert "Cannot sign in" not in _page(admin, "/admin/users").text

    def test_switching_off_asks_first_and_names_them(self, admin: TestClient) -> None:
        _enable(admin)
        _make(admin, ADA, SignInMethod.SSO_ONLY)
        invite(admin, GRACE, method=SignInMethod.SSO_ONLY)
        asked = _save(admin)  # enabled left out: switch it off
        assert asked.status_code == 409
        assert ADA in asked.text and GRACE in asked.text
        assert 'name="confirm" value="yes"' in asked.text
        assert _settings(admin).settings.enabled  # nothing changed yet
        done = _save(admin, confirm="yes")
        assert done.status_code == 303
        assert not _settings(admin).settings.enabled

    def test_switching_off_without_them_does_not_ask(self, admin: TestClient) -> None:
        _enable(admin)
        assert _save(admin).status_code == 303
        assert not _settings(admin).settings.enabled
