"""Tests for personal-mode login codes (AuthManager)."""

from __future__ import annotations

import logging
import threading
import time

import pytest

import spark.web.auth as auth_module
from spark.web.auth import AuthManager, LoginPolicy, RedeemOutcome

CLIENT = "192.0.2.10"
OTHER_CLIENT = "192.0.2.20"


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def _manager(clock: FakeClock, **policy: float) -> AuthManager:
    return AuthManager(LoginPolicy(**policy), clock=clock)  # type: ignore[arg-type]


class TestCodeFormat:
    def test_code_is_eight_uppercase_hex_characters(self, clock: FakeClock) -> None:
        code = _manager(clock).generate_code()
        assert len(code) == 8
        assert code == code.upper()
        int(code, 16)


class TestRedeem:
    def test_valid_code_is_accepted(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.generate_code()
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED

    def test_case_and_surrounding_space_are_ignored(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.generate_code()
        assert auth.redeem(f"  {code.lower()} ", CLIENT) is RedeemOutcome.ACCEPTED

    def test_wrong_code_is_refused(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        auth.generate_code()
        assert auth.redeem("ZZZZZZZZ", CLIENT) is RedeemOutcome.REFUSED

    def test_empty_code_is_refused(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        auth.generate_code()
        assert auth.redeem("", CLIENT) is RedeemOutcome.REFUSED

    def test_no_code_issued_refuses(self, clock: FakeClock) -> None:
        assert _manager(clock).redeem("ABCDEF01", CLIENT) is RedeemOutcome.REFUSED

    def test_non_ascii_input_is_refused_not_raised(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        auth.generate_code()
        assert auth.redeem("ÄÖÜ☃", CLIENT) is RedeemOutcome.REFUSED


class TestSingleUse:
    def test_code_is_redeemable_once_only(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.generate_code()
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED
        assert auth.redeem(code, CLIENT) is RedeemOutcome.REFUSED

    def test_second_client_cannot_reuse_a_redeemed_code(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.generate_code()
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED
        assert auth.redeem(code, OTHER_CLIENT) is RedeemOutcome.REFUSED

    def test_new_code_replaces_the_previous_one(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        first = auth.generate_code()
        second = auth.generate_code()
        assert first != second
        assert auth.redeem(first, CLIENT) is RedeemOutcome.REFUSED
        assert auth.redeem(second, CLIENT) is RedeemOutcome.ACCEPTED

    def test_parallel_redemption_of_one_code_admits_exactly_one(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=1000)
        code = auth.generate_code()
        workers = 32
        barrier = threading.Barrier(workers)
        outcomes: list[RedeemOutcome] = []
        lock = threading.Lock()

        def attempt(n: int) -> None:
            barrier.wait()
            result = auth.redeem(code, f"192.0.2.{n}")
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcomes.count(RedeemOutcome.ACCEPTED) == 1
        assert outcomes.count(RedeemOutcome.REFUSED) == workers - 1


class TestExpiry:
    def test_code_is_accepted_before_it_expires(self, clock: FakeClock) -> None:
        auth = _manager(clock, code_ttl_seconds=60)
        code = auth.generate_code()
        clock.advance(59)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED

    def test_code_is_refused_after_it_expires(self, clock: FakeClock) -> None:
        auth = _manager(clock, code_ttl_seconds=60)
        code = auth.generate_code()
        clock.advance(61)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.REFUSED

    def test_default_lifetime_is_short(self) -> None:
        assert 0 < LoginPolicy().code_ttl_seconds <= 600


class TestEnsureCode:
    def test_issues_a_code_when_none_is_live(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.ensure_code()
        assert code is not None
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED

    def test_issues_nothing_while_a_code_is_live(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.generate_code()
        assert auth.ensure_code() is None
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED

    def test_issues_a_new_code_once_the_live_one_is_spent(self, clock: FakeClock) -> None:
        auth = _manager(clock)
        code = auth.generate_code()
        auth.redeem(code, CLIENT)
        fresh = auth.ensure_code()
        assert fresh is not None and fresh != code

    def test_parallel_callers_issue_one_code(
        self, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_token_hex = auth_module.secrets.token_hex

        def slow_token_hex(nbytes: int) -> str:
            time.sleep(0.01)  # widen any check-then-issue window
            return real_token_hex(nbytes)

        monkeypatch.setattr(auth_module.secrets, "token_hex", slow_token_hex)
        auth = _manager(clock)
        workers = 32
        barrier = threading.Barrier(workers)
        issued: list[str] = []
        lock = threading.Lock()

        def call() -> None:
            barrier.wait()
            code = auth.ensure_code()
            if code is not None:
                with lock:
                    issued.append(code)

        threads = [threading.Thread(target=call) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(issued) == 1
        assert auth.redeem(issued[0], CLIENT) is RedeemOutcome.ACCEPTED

    def test_issues_a_new_code_once_the_live_one_expires(self, clock: FakeClock) -> None:
        auth = _manager(clock, code_ttl_seconds=60)
        auth.generate_code()
        clock.advance(61)
        assert auth.ensure_code() is not None


class TestRateLimit:
    def test_lock_out_after_max_failures_refuses_even_the_right_code(
        self, clock: FakeClock
    ) -> None:
        auth = _manager(clock, max_failures=3)
        code = auth.generate_code()
        for _ in range(3):
            assert auth.redeem("00000000", CLIENT) is RedeemOutcome.REFUSED
        assert auth.redeem(code, CLIENT) is RedeemOutcome.LOCKED_OUT
        assert auth.is_locked_out(CLIENT)

    def test_lock_out_does_not_spend_the_code(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=2)
        code = auth.generate_code()
        auth.redeem("00000000", CLIENT)
        auth.redeem("00000000", CLIENT)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.LOCKED_OUT
        assert auth.redeem(code, OTHER_CLIENT) is RedeemOutcome.ACCEPTED

    def test_lock_out_is_per_client_address(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=2)
        code = auth.generate_code()
        auth.redeem("00000000", CLIENT)
        auth.redeem("00000000", CLIENT)
        assert not auth.is_locked_out(OTHER_CLIENT)
        assert auth.redeem(code, OTHER_CLIENT) is RedeemOutcome.ACCEPTED

    def test_lock_out_ends_after_its_window(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=2, lockout_seconds=600, code_ttl_seconds=3600)
        code = auth.generate_code()
        auth.redeem("00000000", CLIENT)
        auth.redeem("00000000", CLIENT)
        clock.advance(599)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.LOCKED_OUT
        clock.advance(2)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED

    def test_failures_outside_the_window_do_not_count(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=2, failure_window_seconds=60, code_ttl_seconds=3600)
        code = auth.generate_code()
        auth.redeem("00000000", CLIENT)
        clock.advance(61)
        auth.redeem("00000000", CLIENT)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED

    def test_success_clears_earlier_failures(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=2)
        code = auth.generate_code()
        auth.redeem("00000000", CLIENT)
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED
        auth.generate_code()
        auth.redeem("00000000", CLIENT)
        assert not auth.is_locked_out(CLIENT)

    def test_parallel_failures_lock_out_at_the_limit(self, clock: FakeClock) -> None:
        auth = _manager(clock, max_failures=5)
        code = auth.generate_code()
        workers = 40
        barrier = threading.Barrier(workers)

        def attempt() -> None:
            barrier.wait()
            auth.redeem("00000000", CLIENT)

        threads = [threading.Thread(target=attempt) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert auth.redeem(code, CLIENT) is RedeemOutcome.LOCKED_OUT


class TestLogging:
    def test_failures_are_logged_without_the_code(
        self, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        auth = _manager(clock, max_failures=2)
        live = auth.generate_code()
        with caplog.at_level(logging.DEBUG, logger="spark.web.auth"):
            auth.redeem("C0FFEE99", CLIENT)
            auth.redeem("C0FFEE98", CLIENT)
            auth.redeem(live, CLIENT)
        text = caplog.text
        assert CLIENT in text
        assert "locked" in text.lower()
        for secret in ("C0FFEE99", "C0FFEE98", live):
            assert secret not in text
            assert secret.lower() not in text


class TestConstantTime:
    def test_redeem_compares_with_compare_digest(
        self, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, object]] = []
        real = auth_module.hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)
        auth = _manager(clock)
        code = auth.generate_code()
        assert auth.redeem("00000000", CLIENT) is RedeemOutcome.REFUSED
        assert auth.redeem(code, CLIENT) is RedeemOutcome.ACCEPTED
        assert len(calls) >= 2
