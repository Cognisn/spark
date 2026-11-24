"""Test-only helpers: a software authenticator for SPARK's passkey screens.

:class:`SoftAuthenticator` answers ``navigator.credentials.create()`` and
``get()`` as a browser with one authenticator would: it reads the JSON options
SPARK's options routes return and builds the JSON response ``webauthn.js``
posts back, with a real P-256 key, "none" attestation and the counter
incrementing on each use. It is built on fido2's WebAuthn types (already
installed through warden), so warden checks every response exactly as it
checks a browser's. Keys are synthetic and live only for the test.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from fido2.cose import ES256
from fido2.utils import sha256, websafe_decode, websafe_encode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)

from spark.web.security import CSRF_HEADER
from tests.identity_screen_helpers import ORIGIN, form_token
from tests.identity_web_helpers import PUBLIC

_UP, _UV, _AT = 0x01, 0x04, 0x40


@dataclass
class _Key:
    credential_id: bytes
    private_key: ec.EllipticCurvePrivateKey
    rp_id: str
    user_handle: bytes
    resident: bool
    counter: int = 0


@dataclass
class SoftAuthenticator:
    """One authenticator: a platform passkey by default (resident key, verifies the user).

    ``resident=False`` and ``verifies_user=False`` make it a plain security key.
    ``origin`` is the page's origin the client data names.
    """

    origin: str = PUBLIC
    resident: bool = True
    verifies_user: bool = True
    keys: list[_Key] = field(default_factory=list)

    def _flags(self, *extra: int) -> int:
        flags = _UP | (_UV if self.verifies_user else 0)
        for bit in extra:
            flags |= bit
        return flags

    def create(self, options: dict[str, Any]) -> dict[str, Any]:
        """The browser's answer to ``navigator.credentials.create({publicKey: options})``."""
        rp_id = options["rp"]["id"]
        excluded = {d["id"] for d in options.get("excludeCredentials") or []}
        if any(websafe_encode(k.credential_id) in excluded for k in self.keys):
            raise ValueError("InvalidStateError: already registered")
        private_key = ec.generate_private_key(ec.SECP256R1())
        credential_id = os.urandom(32)
        key = _Key(
            credential_id,
            private_key,
            rp_id,
            websafe_decode(options["user"]["id"]),
            self.resident,
        )
        attested = AttestedCredentialData.create(
            Aaguid(bytes(16)), credential_id, ES256.from_cryptography_key(private_key.public_key())
        )
        auth_data = AuthenticatorData.create(
            sha256(rp_id.encode()), self._flags(_AT), key.counter, attested
        )
        client_data = CollectedClientData.create(
            type=CollectedClientData.TYPE.CREATE,
            challenge=websafe_decode(options["challenge"]),
            origin=self.origin,
        )
        self.keys.append(key)
        return {
            "id": websafe_encode(credential_id),
            "rawId": websafe_encode(credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": websafe_encode(client_data),
                "attestationObject": websafe_encode(
                    AttestationObject.create("none", auth_data, {})
                ),
                "transports": ["internal"],
            },
            "clientExtensionResults": {"credProps": {"rk": self.resident}},
        }

    def get(self, options: dict[str, Any]) -> dict[str, Any]:
        """The browser's answer to ``navigator.credentials.get({publicKey: options})``."""
        rp_id = options.get("rpId")
        allowed = {d["id"] for d in options.get("allowCredentials") or []}
        candidates = [
            k
            for k in self.keys
            if k.rp_id == rp_id
            and (websafe_encode(k.credential_id) in allowed if allowed else k.resident)
        ]
        if not candidates:
            raise ValueError("NotAllowedError: no credential for this site")
        key = candidates[-1]
        key.counter += 1
        auth_data = AuthenticatorData.create(sha256(rp_id.encode()), self._flags(), key.counter)
        client_data = CollectedClientData.create(
            type=CollectedClientData.TYPE.GET,
            challenge=websafe_decode(options["challenge"]),
            origin=self.origin,
        )
        signature = key.private_key.sign(
            bytes(auth_data) + client_data.hash, ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": websafe_encode(key.credential_id),
            "rawId": websafe_encode(key.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": websafe_encode(client_data),
                "authenticatorData": websafe_encode(auth_data),
                "signature": websafe_encode(signature),
                "userHandle": websafe_encode(key.user_handle) if key.resident else None,
            },
            "clientExtensionResults": {},
        }


def options_from(
    client: TestClient, path: str, page: Any, body: dict[str, Any] | None = None
) -> Any:
    """POST to an options route as ``webauthn.js`` does: the page's token in the header."""
    return client.post(
        path,
        json=body or {},
        headers={**ORIGIN, CSRF_HEADER: form_token(page)},
    )


def public_key(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, (resp.status_code, resp.text)
    assert resp.headers.get("cache-control") == "no-store"
    options: dict[str, Any] = resp.json()["publicKey"]
    return options


def as_field(credential: dict[str, Any]) -> str:
    """The credential as webauthn.js puts it in the form's hidden field."""
    return json.dumps(credential)


def form_action_with(page: Any, marker: str) -> str:
    """The action of the form carrying ``data-webauthn="<marker>"``."""
    match = re.search(rf'<form[^>]*data-webauthn="{marker}"[^>]*>', page.text)
    assert match, page.text
    action = re.search(r'action="([^"]+)"', match.group(0))
    assert action, match.group(0)
    return action.group(1)


def options_url(page: Any, marker: str) -> str:
    match = re.search(rf'<form[^>]*data-webauthn="{marker}"[^>]*>', page.text)
    assert match, page.text
    url = re.search(r'data-options-url="([^"]+)"', match.group(0))
    assert url, match.group(0)
    return url.group(1)
