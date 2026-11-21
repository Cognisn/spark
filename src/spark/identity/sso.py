"""Single sign-on settings as SPARK's screens use them (server mode).

warden owns single sign-on: its settings (``SsoSettingsStore``: enabled, the
issuer URL and the client ID, run-time overrides over ``warden.sso``), the
client secret (written to konfig's secrets store under the name in
``warden.sso.client_secret`` and never read back out) and the check of a
configuration against the provider. warden audits every change itself with
field and secret names only, never values. This module is SPARK's view of it:

* :func:`state` says whether single sign-on is *available*: switched on, with
  an issuer, a client ID and a stored client secret. Every SSO button,
  wording and sign-in method choice on SPARK's screens follows it, so nothing
  offers single sign-on that cannot work.
* :func:`save` writes an administrator's changes: the secret first (when a
  new one is given), then the settings, refusing to switch single sign-on on
  without a secret.
* :func:`sso_only_users` names the users who can sign in only through single
  sign-on, for the confirmation before switching it off and the flags on the
  Users page.
* :func:`check_text` words warden's configuration report from its fixed check
  names and reasons (``CHECKS``, ``REASONS``); nothing the provider sent is
  shown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final

from warden.identity import SignInMethod, User, UserStatus
from warden.identity.sso.settings import SsoSettings, SsoSettingsError

__all__ = [
    "CHECKS",
    "REASONS",
    "SECRET_REFUSED",
    "SECRET_REQUIRED",
    "SsoChange",
    "SsoState",
    "available",
    "check_text",
    "save",
    "sso_only_users",
    "state",
]

logger = logging.getLogger(__name__)

SECRET_REQUIRED: Final = "Store a client secret before switching single sign-on on."
SECRET_REFUSED: Final = "The client secret must be 1 to 4096 printable characters."

# warden's configuration checks (SingleSignOn.test_configuration), by name.
CHECKS: Final[dict[str, str]] = {
    "settings": "Issuer URL and client ID",
    "client_secret": "Client secret",
    "discovery": "Discovery document",
    "algorithms": "Signing algorithms",
    "jwks": "Signing keys (JWKS)",
}
# warden's fixed reasons for a failed check; anything else gets the general text.
REASONS: Final[dict[str, str]] = {
    "missing": "Not set.",
    "discovery_unavailable": (
        "The issuer's discovery document could not be fetched. Check the issuer URL and "
        "that this server can reach the provider over https."
    ),
    "issuer_mismatch": (
        "The discovery document names a different issuer. Enter the issuer exactly as the "
        "provider states it, trailing slash included."
    ),
    "discovery_invalid": (
        "The discovery document is not usable: its endpoints must use https and it must "
        "support PKCE (S256)."
    ),
    "none_shared": "The provider signs ID tokens with none of the algorithms Spark accepts.",
    "jwks_unavailable": "The provider's signing keys could not be fetched.",
    "no_usable_key": "The provider publishes no signing key Spark can use.",
}
_FAILED: Final = "Failed."
_PASSED: Final = "Passed."
# Users who can still sign in, or will once they accept an invitation.
_COUNTED = (UserStatus.ACTIVE, UserStatus.SUSPENDED, UserStatus.INVITED)


@dataclass(frozen=True, slots=True)
class SsoState:
    """The effective settings and whether a client secret is stored (never the secret)."""

    settings: SsoSettings
    secret_set: bool

    @property
    def configured(self) -> bool:
        """An issuer, a client ID and a client secret: enough to sign in once switched on."""
        s = self.settings
        return bool(s.issuer_url and s.client_id and self.secret_set)

    @property
    def available(self) -> bool:
        """Switched on and configured: the screens offer single sign-on only then."""
        return bool(self.settings.enabled and self.configured)


@dataclass(frozen=True, slots=True)
class SsoChange:
    """An administrator's change. ``client_secret`` None keeps the stored secret."""

    issuer_url: str | None
    client_id: str | None
    enabled: bool
    client_secret: str | None = None


async def state(identity: Any) -> SsoState:
    """warden's effective settings, and whether its client secret is stored."""
    sso = identity.sso
    settings: SsoSettings = await sso.settings.current()
    # konfig may reach a remote store: off the event loop.
    secret_set = bool(await asyncio.to_thread(sso.client_secret_set))
    return SsoState(settings, secret_set)


async def available(identity: Any) -> bool:
    """Whether to offer single sign-on; False (logged) if the settings cannot be read."""
    try:
        return (await state(identity)).available
    except Exception:  # a settings read failing must not break sign-in
        logger.warning("Could not read the single sign-on settings")
        return False


async def save(identity: Any, change: SsoChange, *, actor: str, source_ip: Any) -> SsoSettings:
    """Store a new client secret (if given), then the settings; warden audits both.

    Raises ``ValueError`` with a fixed message (never a value) when the change is
    refused: a secret warden does not accept, switching on without a secret, or
    a setting warden's rules refuse (its message names fields only).
    """
    sso = identity.sso
    if change.client_secret is not None:
        try:
            await sso.set_client_secret(change.client_secret, actor=actor, source_ip=source_ip)
        except ValueError:
            raise ValueError(SECRET_REFUSED) from None
    if change.enabled and not await asyncio.to_thread(sso.client_secret_set):
        raise ValueError(SECRET_REQUIRED)
    try:
        settings: SsoSettings = await sso.settings.update(
            actor=actor,
            source_ip=source_ip,
            enabled=change.enabled,
            issuer_url=change.issuer_url,
            client_id=change.client_id,
        )
    except SsoSettingsError as exc:
        raise ValueError(_settings_refusal(str(exc))) from None
    return settings


def _settings_refusal(message: str) -> str:
    """warden's refusal (field names only), worded for the screen."""
    names = {
        "issuer_url": "issuer URL",
        "client_id": "client ID",
        "http.sso_completion_link": "the completion link",
    }
    head, _, fields = message.partition(": ")
    worded = ", ".join(names.get(f.strip(), f.strip()) for f in fields.split(",") if f.strip())
    if head == "required while enabled":
        return f"Switching single sign-on on needs: {worded}."
    if head == "invalid settings":
        return f"Not accepted: {worded}. The issuer URL must be an https URL."
    return "The settings were not accepted."


async def sso_only_users(identity: Any) -> list[User]:
    """Users (active, suspended or invited) who can sign in only through single sign-on."""
    users: list[User] = await identity.users.list()
    return [u for u in users if u.sign_in_method is SignInMethod.SSO_ONLY and u.status in _COUNTED]


def check_text(name: str, ok: bool, reason: str | None) -> tuple[str, str]:
    """A check's label and result, worded from warden's fixed names and reasons."""
    label = CHECKS.get(name, name)
    if ok:
        return label, _PASSED
    return label, REASONS.get(reason or "", _FAILED)
