"""Which budget applies, the 80% warning, the 100% stop and the monthly reset (A14)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

from spark import mode
from spark.database import usage
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.usage import budgets
from spark.usage.budgets import BudgetExceeded, BudgetStatus

NOV = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
NOV_FIRST = datetime(2026, 11, 1, tzinfo=timezone.utc)
OCT_LAST = datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)


@pytest.fixture()
def db(spark_db):
    return spark_db


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


@pytest.fixture()
def personal_mode(tmp_path: Any) -> Iterator[None]:
    mode.configure(
        RuntimeSettings(
            Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "x.db")
        )
    )
    try:
        yield
    finally:
        mode.reset()


class TestStatus:
    def test_unlimited(self) -> None:
        status = BudgetStatus("2026-11", used=10**9, limit=None)
        assert status.state == "unlimited"
        assert status.percent is None

    @pytest.mark.parametrize(
        ("used", "state"),
        [(0, "ok"), (799, "ok"), (800, "warning"), (999, "warning"), (1000, "exceeded")],
    )
    def test_warning_at_80_and_stop_at_100_percent(self, used: int, state: str) -> None:
        assert BudgetStatus("2026-11", used=used, limit=1000).state == state

    def test_percent(self) -> None:
        assert BudgetStatus("2026-11", used=250, limit=1000).percent == 25.0

    def test_resets_at_midnight_utc_on_the_first_of_next_month(self) -> None:
        status = BudgetStatus("2026-12", used=0, limit=1)
        assert status.resets_at == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_the_refusal_message_is_clear(self) -> None:
        message = BudgetStatus("2026-11", used=1200, limit=1000).refusal_message()
        assert "monthly token budget" in message
        assert "1,200 of 1,000" in message
        assert "1 December 2026" in message
        assert "UTC" in message

    def test_as_dict(self) -> None:
        assert BudgetStatus("2026-11", used=850, limit=1000).as_dict() == {
            "period": "2026-11",
            "used": 850,
            "limit": 1000,
            "percent": 85.0,
            "state": "warning",
            "resets_at": "2026-12-01T00:00:00+00:00",
        }


class TestServerLimit:
    async def test_no_budgets_is_unlimited(self, db, server_mode) -> None:
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV)
        assert status.limit is None and status.state == "unlimited"

    async def test_the_role_budget_applies(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        assert (await budgets.status_for(db, "u1", {"user"}, now=NOV)).limit == 1000

    async def test_a_role_the_user_does_not_hold_does_not_apply(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        assert (await budgets.status_for(db, "u1", {"power_user"}, now=NOV)).limit is None

    async def test_the_most_generous_set_budget_of_the_users_roles(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.set_role_budget(db, "power_user", 5000)
        status = await budgets.status_for(db, "u1", {"user", "power_user", "auditor"}, now=NOV)
        assert status.limit == 5000

    async def test_the_users_override_wins_over_their_roles(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.set_user_override(db, "u1", 50)
        assert (await budgets.status_for(db, "u1", {"user"}, now=NOV)).limit == 50

    async def test_an_override_may_raise_the_limit(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.set_user_override(db, "u1", 9000)
        assert (await budgets.status_for(db, "u1", {"user"}, now=NOV)).limit == 9000

    async def test_an_unlimited_override_wins_over_their_roles(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.set_user_override(db, "u1", None)
        assert (await budgets.status_for(db, "u1", {"user"}, now=NOV)).limit is None

    async def test_another_users_override_does_not_apply(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.set_user_override(db, "u2", None)
        assert (await budgets.status_for(db, "u1", {"user"}, now=NOV)).limit == 1000

    async def test_no_roles_means_only_an_override_applies(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        assert (await budgets.status_for(db, "u1", None, now=NOV)).limit is None


class TestPersonalLimit:
    async def test_no_budget_unless_set(self, db, personal_mode) -> None:
        await usage.record_usage(db, 1, "m", 10**6, 0, "local", at=NOV)
        status = await budgets.status_for(db, "local", None, now=NOV)
        assert status.state == "unlimited"
        assert status.used == 10**6

    async def test_role_budgets_do_not_apply(self, db, personal_mode) -> None:
        # The local operator holds the administrator role, but personal mode has no roles to budget.
        await usage.set_role_budget(db, "administrator", 10)
        assert (await budgets.status_for(db, "local", {"administrator"}, now=NOV)).limit is None

    async def test_the_local_users_own_budget_applies_once_set(self, db, personal_mode) -> None:
        await usage.set_user_override(db, "local", 100)
        assert (await budgets.status_for(db, "local", None, now=NOV)).limit == 100


class TestUsedThisMonth:
    async def test_counts_this_months_calls(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.record_usage(db, 1, "m", 300, 200, "u1", at=NOV_FIRST)
        await usage.record_usage(db, 1, "m", 100, 0, "u2", at=NOV_FIRST)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV)
        assert (status.period, status.used) == ("2026-11", 500)

    async def test_the_budget_resets_at_the_utc_month_boundary(self, db, server_mode) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.record_usage(db, 1, "m", 1000, 0, "u1", at=OCT_LAST)
        october = await budgets.status_for(db, "u1", {"user"}, now=OCT_LAST)
        november = await budgets.status_for(db, "u1", {"user"}, now=NOV_FIRST)
        assert october.state == "exceeded"
        assert (november.used, november.state) == (0, "ok")


class TestCheck:
    """The synchronous check a turn makes before it starts."""

    def test_under_budget_passes_and_returns_the_status(self, sync_db, server_mode) -> None:
        sync_db.call(usage.set_role_budget, "user", 1000)
        sync_db.call(usage.record_usage, 1, "m", 799, 0, "u1", at=NOV)
        status = budgets.check(sync_db, "u1", {"user"}, now=NOV)
        assert status.state == "ok"

    def test_warning_still_passes(self, sync_db, server_mode) -> None:
        sync_db.call(usage.set_role_budget, "user", 1000)
        sync_db.call(usage.record_usage, 1, "m", 800, 0, "u1", at=NOV)
        assert budgets.check(sync_db, "u1", {"user"}, now=NOV).state == "warning"

    def test_at_100_percent_a_new_request_is_refused(self, sync_db, server_mode) -> None:
        sync_db.call(usage.set_role_budget, "user", 1000)
        sync_db.call(usage.record_usage, 1, "m", 600, 400, "u1", at=NOV)
        with pytest.raises(BudgetExceeded) as raised:
            budgets.check(sync_db, "u1", {"user"}, now=NOV)
        assert raised.value.status.used == 1000
        assert "monthly token budget" in str(raised.value)

    def test_the_refusal_is_audited(self, sync_db, server_mode, monkeypatch) -> None:
        from spark.identity import audit

        events: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            audit, "submit", lambda action, outcome="success", **f: events.append((action, f))
        )
        sync_db.call(usage.set_role_budget, "user", 10)
        sync_db.call(usage.record_usage, 1, "m", 10, 0, "u1", at=NOV)
        with pytest.raises(BudgetExceeded):
            budgets.check(sync_db, "u1", {"user"}, now=NOV)
        [(action, fields)] = events
        assert action == audit.USAGE_REFUSED
        assert fields["user_guid"] == "u1"
        assert fields["details"] == {"used": 10, "limit": 10, "period": "2026-11"}

    def test_personal_mode_without_a_budget_never_refuses(self, sync_db, personal_mode) -> None:
        sync_db.call(usage.record_usage, 1, "m", 10**9, 0, "local", at=NOV)
        assert budgets.check(sync_db, "local", None, now=NOV).state == "unlimited"
