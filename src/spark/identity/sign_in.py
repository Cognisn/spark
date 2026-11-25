"""How sign-in methods and their settings are worded on SPARK's screens (server mode).

warden 0.2.0 holds the rules (rulings CS13 to CS18): an instance default method
every user follows unless they have one of their own, the second factors the
instance allows, and pending changes that wait for a credential. This module
holds only the words SPARK shows for them: each method's label and plain
description, the factors, and the fixed wording of every refusal, built from
warden's fixed reasons and never from anything a request sent.

Labels say ``SSO`` and ``MFA`` rather than spelling out single sign-on, so a
screen that offers no single sign-on (it is not set up) never names it for a
user who follows the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from warden.identity import SecondFactorKind, SignInMethod

__all__ = [
    "DEFAULT_CHOICE",
    "FACTORS",
    "METHODS",
    "MethodText",
    "choices",
    "factor_label",
    "in_force_label",
    "method_label",
    "method_text",
    "refusal_text",
]

# The form value that has a user follow the instance default.
DEFAULT_CHOICE: Final = "default"


@dataclass(frozen=True, slots=True)
class MethodText:
    """A method's label and what it means, in plain words."""

    method: SignInMethod
    label: str
    description: str


METHODS: Final[tuple[MethodText, ...]] = (
    MethodText(
        SignInMethod.PASSWORD,
        "Password + MFA",
        "A password and a second factor. Single sign-on is not offered.",
    ),
    MethodText(
        SignInMethod.PASSKEY_ONLY,
        "Passkey",
        "A passkey alone, with no password. Needs passkeys to work on this server.",
    ),
    MethodText(
        SignInMethod.SSO_ONLY,
        "SSO",
        "Single sign-on through your identity provider, with no password and no passkey. "
        "Needs single sign-on to be set up and switched on.",
    ),
    MethodText(
        SignInMethod.PASSWORD_AND_SSO,
        "Password + MFA or SSO",
        "Either a password and a second factor, or single sign-on once it is set up.",
    ),
)
_BY_METHOD: Final = {text.method: text for text in METHODS}

FACTORS: Final[tuple[tuple[SecondFactorKind, str, str], ...]] = (
    (
        SecondFactorKind.TOTP,
        "Authenticator app",
        "A six-digit code from an app on the person's phone.",
    ),
    (
        SecondFactorKind.PASSKEY,
        "Passkey or security key",
        "A passkey or a hardware security key. Needs passkeys to work on this server.",
    ),
)
_FACTOR_LABELS: Final = {kind: label for kind, label, _ in FACTORS}


def method_text(method: SignInMethod) -> MethodText:
    """The wording for ``method``."""
    return _BY_METHOD[method]


def method_label(method: SignInMethod | str | None) -> str:
    """A method's short label; empty for none (a user who follows the default)."""
    if method is None:
        return ""
    try:
        return _BY_METHOD[SignInMethod(method)].label
    except ValueError:
        return ""


def in_force_label(method: SignInMethod | str, sso_available: bool) -> str:
    """How a user in fact signs in: a method that includes single sign-on is a password until it is set up."""
    if SignInMethod(method) is SignInMethod.PASSWORD_AND_SSO and not sso_available:
        return method_label(SignInMethod.PASSWORD)
    return method_label(method)


def factor_label(kind: SecondFactorKind | str) -> str:
    """A second factor's label."""
    return _FACTOR_LABELS.get(SecondFactorKind(kind), str(kind))


def choices(*, sso_available: bool, webauthn: bool) -> list[MethodText]:
    """The methods a screen offers: single sign-on only once set up, passkeys only where they work."""
    offered = []
    for text in METHODS:
        if text.method is SignInMethod.SSO_ONLY and not sso_available:
            continue
        if text.method is SignInMethod.PASSKEY_ONLY and not webauthn:
            continue
        offered.append(text)
    return offered


# What warden's refusals (SignInMethodChangeRefused.reason) mean, in plain words.
_REFUSALS: Final[dict[str, str]] = {
    "sso_not_configured": (
        "Single sign-on is not set up. Set it up and switch it on first, on the "
        "Single sign-on page."
    ),
    "webauthn": "Passkeys cannot be used on this server, so that choice is not available.",
    "mail_sender": (
        "This change needs a set-up link emailed to the person, and no mail server is "
        "set up. Set one up on the Mail page first. Nothing was changed."
    ),
    "passkey_registration_link": (
        "This change needs a passkey registration link, and this server has none "
        "configured. Nothing was changed."
    ),
    "reset_password_link": (
        "This change needs a set-password link, and this server has none configured. "
        "Nothing was changed."
    ),
    "not_pending": "No sign-in method change is waiting for this person.",
    "no_link": (
        "This change needs no link: it completes the first time the person signs in "
        "through single sign-on."
    ),
    "user_inactive": "Set-up links go to active users only.",
}
_REFUSED_GENERAL: Final = "That sign-in change was not accepted. Nothing was changed."
HOLDS_KEYS: Final = (
    "This user has a passkey or security key, and single sign-on only allows neither. "
    "Remove their keys first (reset their second factors), then try again."
)
LINK_NOT_SENT: Final = "The set-up link could not be sent, so nothing was changed."
NO_FACTOR: Final = "Allow at least one second factor."
FACTORS_REFUSED: Final = "That choice of second factors was not accepted. Nothing was changed."
METHOD_REFUSED: Final = "That is not a sign-in method to set."
SSO_OFF: Final = "Single sign-on is not switched on, so it cannot be chosen yet."


def refusal_text(reason: str) -> str:
    """The words for one of warden's refusal reasons (anything unknown gets the general text)."""
    return _REFUSALS.get(reason, _REFUSED_GENERAL)
