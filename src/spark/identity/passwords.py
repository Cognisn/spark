"""warden's password refusals and forced changes, worded for SPARK's screens.

warden enforces a password policy wherever a password is set (an invitation,
a reset, the user's own change and the forced change) and refuses a password
it does not accept with ``PasswordRejected``, which names every failure with a
stable code and a plain message. :func:`refusal_text` turns that into the one
line a screen shows.

At sign-in warden may require a new password before it issues a session
(``PasswordChangeIssued``): an administrator asked for it, the password
expired, it no longer meets the policy, or it is on a watch list.
:func:`change_reason_text` gives the fixed message for each reason; a reason
that is not one of warden's gets the general message, so nothing from a URL
is ever shown.
"""

from __future__ import annotations

from typing import Final

from warden.identity import ChangeReason, PasswordRejected, UserError

__all__ = [
    "CHANGE_REASONS",
    "CHANGE_REQUIRED",
    "NOT_ACCEPTED",
    "PASSWORDS_DIFFER",
    "change_reason_text",
    "refusal_text",
]

NOT_ACCEPTED: Final = "The password was not accepted. Choose another."
PASSWORDS_DIFFER: Final = "The two passwords do not match."
CHANGE_REQUIRED: Final = "Choose a new password to finish signing in."
CHANGE_REASONS: Final[dict[ChangeReason, str]] = {
    ChangeReason.ADMINISTRATOR: (
        "An administrator asks you to choose a new password before you continue."
    ),
    ChangeReason.EXPIRED: "Your password has expired. Choose a new one to continue.",
    ChangeReason.POLICY: (
        "Your password no longer meets this server's password policy. "
        "Choose a new one to continue."
    ),
    ChangeReason.WATCH_LIST: (
        "Your password is on a list of passwords that must not be used. "
        "Choose a new one to continue."
    ),
}


def refusal_text(exc: UserError) -> str:
    """The line a screen shows for a password warden refused (or another user error)."""
    if not isinstance(exc, PasswordRejected):
        return str(exc)
    messages = [f.message.strip().rstrip(".") for f in exc.failures if f.message.strip()]
    return ". ".join(messages) + "." if messages else NOT_ACCEPTED


def change_reason_text(reason: str | None) -> str:
    """The fixed message for why a new password is needed; the general one otherwise."""
    try:
        return CHANGE_REASONS[ChangeReason(reason or "")]
    except ValueError:
        return CHANGE_REQUIRED
