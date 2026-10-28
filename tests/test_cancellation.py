"""Tests for the CancellationToken primitive."""

from __future__ import annotations

from spark.core.cancellation import CancellationToken


class TestCancellationToken:
    def test_starts_uncancelled(self) -> None:
        tok = CancellationToken()
        assert tok.is_cancelled() is False
        assert tok.reason == ""

    def test_cancel_sets_flag(self) -> None:
        tok = CancellationToken()
        tok.cancel("user")
        assert tok.is_cancelled() is True
        assert tok.reason == "user"

    def test_cancel_default_reason(self) -> None:
        tok = CancellationToken()
        tok.cancel()
        assert tok.is_cancelled() is True
        assert tok.reason == ""

    def test_cancel_is_idempotent(self) -> None:
        tok = CancellationToken()
        tok.cancel("first")
        tok.cancel("second")
        assert tok.is_cancelled() is True
        # First reason wins — mutating after the fact would mask the original cause.
        assert tok.reason == "first"
