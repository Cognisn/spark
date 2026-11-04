"""Single sign-on end to end against a live OIDC provider (mock-oauth2-server).

Marked ``integration``: CI deselects it, and it runs only when Matthew starts it
with a provider. It is skipped unless ``SPARK_TEST_SSO_ISSUER`` names the
provider's issuer, for example ``https://localhost:8443/default`` for
navikt's mock-oauth2-server started with TLS and a non-interactive login that
issues ``email`` ``ada@example.com`` with ``email_verified`` true, such as:

    docker run --rm -p 127.0.0.1:8443:8080 -e JSON_CONFIG='{"interactiveLogin": false,
      "httpServer": {"type": "NettyWrapper", "ssl": {}},
      "tokenCallbacks": [{"issuerId": "default", "requestMappings": [{"requestParam":
      "grant_type", "match": "*", "claims": {"sub": "ada", "email": "ada@example.com",
      "email_verified": true, "aud": ["spark"]}}]}]}'
      ghcr.io/navikt/mock-oauth2-server:<pinned version>

The mapping matches on ``grant_type`` because the authorisation code exchange
carries no ``scope`` parameter; mapped on ``scope``, the token endpoint issues a
random subject and no email, and warden refuses the sign-in. ``deploy/live/stack.py``
builds the working configuration the live suite uses.

warden reaches providers over HTTPS only, so ``SSL_CERT_FILE`` must name a PEM
bundle that trusts the mock's certificate (with the public CAs). The browser
leg runs through the real app: SPARK's start and callback routes, warden's
code exchange and id_token validation, and the completion screen.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from spark.identity.web import ACCESS_COOKIE
from tests.identity_screen_helpers import NAVIGATE, plain_client, server_db  # noqa: F401
from tests.identity_web_helpers import PUBLIC, make_user, runtime, set_cookie_headers

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ISSUER = os.environ.get("SPARK_TEST_SSO_ISSUER", "")
ADA = "ada@example.com"


@pytest.mark.skipif(not ISSUER, reason="SPARK_TEST_SSO_ISSUER names no provider")
def test_single_sign_on_against_a_live_provider(plain_client: TestClient) -> None:
    make_user(plain_client, ADA)
    sso = runtime(plain_client).identity.sso

    async def configure() -> None:
        await sso.settings.update(
            actor="test",
            enabled=True,
            issuer_url=ISSUER,
            client_id="spark",
            scopes=["openid", "email"],
        )
        await sso.set_client_secret("spark-test-dev-only", actor="test")

    plain_client.portal.call(configure)  # type: ignore[union-attr]

    start = plain_client.get("/identity/auth/sso/start", follow_redirects=False)
    assert start.status_code == 302, start.text
    with httpx.Client(verify=os.environ.get("SSL_CERT_FILE") or True) as browser:
        at_provider = browser.get(start.headers["location"], follow_redirects=False)
    assert at_provider.status_code in (302, 303), at_provider.text
    callback = at_provider.headers["location"]
    assert callback.startswith(PUBLIC + "/identity/auth/sso/callback?")

    back = plain_client.get(callback[len(PUBLIC) :], follow_redirects=False)
    assert back.status_code == 302
    assert back.headers["location"].startswith("/auth/sso/complete?code="), back.headers

    done: Any = plain_client.get(back.headers["location"], follow_redirects=False)
    assert done.status_code == 200
    assert any(h.startswith(f"{ACCESS_COOKIE}=") for h in set_cookie_headers(done))
    assert plain_client.get("/conversations", headers=NAVIGATE).status_code == 200
