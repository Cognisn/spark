"""warden's password refusals and forced changes, worded for SPARK's screens.

warden 0.1.0 refuses a new password with ``PasswordRejected`` naming every
failure, and may require a change at sign-in (``PasswordChangeIssued``). The
screens show warden's plain failure messages and a fixed message per reason,
never a code or anything taken from a URL or cookie.
"""

from __future__ import annotations

from warden.identity import ChangeReason, PasswordFailure, PasswordRejected, UserError

from spark.identity import passwords


def test_a_refusal_lists_each_failure_in_plain_words() -> None:
    exc = PasswordRejected(
        [
            PasswordFailure("min_length", "Use at least 12 characters"),
            PasswordFailure("watch_list", "This password is too common"),
        ]
    )
    text = passwords.refusal_text(exc)
    assert text == "Use at least 12 characters. This password is too common."
    assert "min_length" not in text and "watch_list" not in text


def test_a_refusal_without_messages_has_the_fixed_text() -> None:
    assert passwords.refusal_text(PasswordRejected([])) == passwords.NOT_ACCEPTED


def test_another_user_error_keeps_its_own_message() -> None:
    assert passwords.refusal_text(UserError("that email address is taken")) == (
        "that email address is taken"
    )


def test_every_change_reason_has_a_fixed_message() -> None:
    for reason in ChangeReason:
        assert passwords.change_reason_text(reason.value) == passwords.CHANGE_REASONS[reason]


def test_an_unknown_reason_gets_the_general_message() -> None:
    assert passwords.change_reason_text("<script>") == passwords.CHANGE_REQUIRED
    assert passwords.change_reason_text(None) == passwords.CHANGE_REQUIRED
