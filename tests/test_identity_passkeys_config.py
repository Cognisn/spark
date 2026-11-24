"""Passkeys' configuration: the relying party and origins follow server.public_base_url.

The rule (recorded as a Skuld decision): with nothing set, the relying party
id is the host of ``server.public_base_url`` and the only allowed origin is
its origin; ``warden.webauthn.allowed_origins``, when set, must include it,
and warden checks that ``rp_id`` covers every allowed origin. Either mismatch
stops start-up with a message naming the settings. On an address whose host
is an IP address there is no relying party, so passkeys are off.
"""

from __future__ import annotations

import pytest

from spark.identity.runtime import PASSKEY_REGISTRATION_LINK, warden_config
from spark.instance_settings.startup import StartupConfigError
from tests.test_identity_runtime import PUBLIC, Konfig, _settings
from tests.test_instance_settings_web import MemorySecrets


def _config(public: str = PUBLIC, **webauthn: object) -> object:
    settings = Konfig({"server": {"public_base_url": public}, "warden": {"webauthn": webauthn}})
    return warden_config(settings, MemorySecrets(), public)


class TestRelyingParty:
    def test_nothing_set_derives_both_from_the_public_address(self) -> None:
        config = warden_config(_settings(), MemorySecrets(), PUBLIC)
        assert config.webauthn.relying_party_id(config.http) == "spark.example.com"
        assert config.webauthn.origins(config.http) == (PUBLIC,)

    def test_the_registration_link_opens_sparks_own_screen(self) -> None:
        config = warden_config(_settings(), MemorySecrets(), PUBLIC)
        assert config.http.passkey_registration_link == PASSKEY_REGISTRATION_LINK
        assert config.http.render_link(config.http.passkey_registration_link, token="t") == (
            f"{PUBLIC}/auth/passkey-registration?token=t"
        )

    def test_a_parent_domain_as_relying_party_is_accepted(self) -> None:
        config = _config(rp_id="example.com")
        assert config.webauthn.relying_party_id(config.http) == "example.com"  # type: ignore[attr-defined]

    def test_origins_that_include_the_public_address_are_accepted(self) -> None:
        other = "https://chat.example.com"
        config = _config(rp_id="example.com", allowed_origins=[PUBLIC, other])
        assert config.webauthn.origins(config.http) == (PUBLIC, other)  # type: ignore[attr-defined]

    def test_origins_without_the_public_address_stop_start_up(self) -> None:
        with pytest.raises(StartupConfigError) as caught:
            _config(rp_id="example.com", allowed_origins=["https://chat.example.com"])
        message = str(caught.value)
        assert "warden.webauthn.allowed_origins" in message
        assert "server.public_base_url" in message and PUBLIC in message

    def test_a_relying_party_that_does_not_cover_the_address_stops_start_up(self) -> None:
        with pytest.raises(StartupConfigError) as caught:
            _config(rp_id="other.example.org")
        assert "rp_id" in str(caught.value)

    def test_an_ip_address_has_no_relying_party(self) -> None:
        config = _config(public="http://127.0.0.1:8080")
        assert config.webauthn.relying_party_id(config.http) is None  # type: ignore[attr-defined]
