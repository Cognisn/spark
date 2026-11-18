"""Credit allowances per connection: which applies, 80%, 100% and the monthly reset (M6 to M10)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from spark import mode
from spark.database import connections as conn_repo
from spark.database import usage
from spark.database.connections import Assignment, ConnectionRow, Metering, ModelRow
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.usage import budgets
from spark.usage.budgets import BudgetExceeded, BudgetStatus

NOV = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
NOV_FIRST = datetime(2026, 11, 1, tzinfo=timezone.utc)
OCT_LAST = datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
WORK = "work"
LAB = "lab"


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


async def add_connection(db: Any, cid: str = WORK, *, metered: bool = True, **rates: Any) -> None:
    """A stored connection (credits: 1 per 1,000 weighted tokens by default)."""
    await conn_repo.save_connection(
        db,
        ConnectionRow(cid, f"{cid.title()} Bedrock", "stub", {"kind": "stub"}, {}),
    )
    await conn_repo.save_metering(db, Metering(cid, metered=metered, **rates))


async def assign(
    db: Any,
    cid: str = WORK,
    *,
    role: str | None = None,
    user: str | None = None,
    credits: int | None = None,
) -> None:
    await conn_repo.save_assignment(
        db, Assignment(cid, role=role, user_guid=user, monthly_credits=credits)
    )


async def spend(db: Any, user: str, credits: int, *, cid: str = WORK, at: datetime = NOV) -> None:
    """Record a call costing ``credits`` at the default rates (1,000 input tokens each)."""
    await usage.record_usage(db, 1, "m", credits * 1000, 0, user, at=at, connection_id=cid)


class TestStatus:
    def test_unlimited(self) -> None:
        status = BudgetStatus("2026-11", used=Decimal(10**9))
        assert status.state == "unlimited"
        assert status.percent is None and status.remaining is None

    @pytest.mark.parametrize(
        ("used", "state"),
        [
            ("0", "ok"),
            ("799.999999", "ok"),
            ("800", "warning"),
            ("999.999999", "warning"),
            ("1000", "exceeded"),
        ],
    )
    def test_warning_at_80_and_stop_at_100_percent(self, used: str, state: str) -> None:
        assert BudgetStatus("2026-11", Decimal(used), Decimal(1000)).state == state

    def test_no_allowance_is_used_up(self) -> None:
        status = BudgetStatus("2026-11", Decimal(0), Decimal(0))
        assert (status.state, status.percent) == ("exceeded", 100.0)

    def test_percent_and_remaining(self) -> None:
        status = BudgetStatus("2026-11", Decimal("250.5"), Decimal(1000))
        assert status.percent == 25.1
        assert status.remaining == Decimal("749.5")
        assert BudgetStatus("2026-11", Decimal(1200), Decimal(1000)).remaining == 0

    def test_resets_at_midnight_utc_on_the_first_of_next_month(self) -> None:
        status = BudgetStatus("2026-12", limit=Decimal(1))
        assert status.resets_at == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_the_refusal_names_the_connection_and_the_reset_date(self) -> None:
        message = BudgetStatus(
            "2026-11", Decimal("1200.5"), Decimal(1000), WORK, "Work Bedrock"
        ).refusal_message()
        assert "monthly credits on Work Bedrock" in message
        assert "1,200.50 of 1,000 credits in November 2026" in message
        assert "00:00 UTC on 1 December 2026" in message

    def test_as_dict(self) -> None:
        status = BudgetStatus("2026-11", Decimal("850.25"), Decimal(1000), WORK, "Work Bedrock")
        assert status.as_dict() == {
            "period": "2026-11",
            "connection_id": WORK,
            "connection": "Work Bedrock",
            "unit": "credits",
            "used": 850.25,
            "limit": 1000.0,
            "remaining": 149.75,
            "percent": 85.0,
            "state": "warning",
            "resets_at": "2026-12-01T00:00:00+00:00",
        }


class TestAllowance:
    async def test_the_role_allowance_applies(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert (status.limit, status.connection) == (Decimal(1000), "Work Bedrock")

    async def test_a_role_the_user_does_not_hold_gives_no_allowance(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        status = await budgets.status_for(db, "u1", {"auditor"}, now=NOV, connection_id=WORK)
        assert (status.limit, status.state) == (Decimal(0), "exceeded")

    async def test_the_highest_allowance_of_the_users_roles(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await assign(db, role="power_user", credits=5000)
        await assign(db, role="auditor", credits=200)
        status = await budgets.status_for(
            db, "u1", {"user", "power_user", "auditor"}, now=NOV, connection_id=WORK
        )
        assert status.limit == Decimal(5000)

    async def test_a_role_without_a_limit_lifts_the_others(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await assign(db, role="power_user", credits=None)
        status = await budgets.status_for(
            db, "u1", {"user", "power_user"}, now=NOV, connection_id=WORK
        )
        assert status.state == "unlimited"

    async def test_the_users_own_assignment_wins_over_their_roles(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await assign(db, user="u1", credits=50)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.limit == Decimal(50)

    async def test_the_users_own_assignment_may_raise_it(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await assign(db, user="u1", credits=9000)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.limit == Decimal(9000)

    async def test_another_users_assignment_does_not_apply(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await assign(db, user="u2", credits=5)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.limit == Decimal(1000)

    async def test_another_connections_allowance_does_not_apply(self, db, server_mode) -> None:
        await add_connection(db)
        await add_connection(db, LAB)
        await assign(db, role="user", credits=1000)
        await assign(db, LAB, role="user", credits=10)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.limit == Decimal(1000)

    async def test_no_roles_means_only_the_users_own_assignment(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        assert (await budgets.status_for(db, "u1", None, now=NOV, connection_id=WORK)).limit == 0

    async def test_a_connection_assigned_to_no_one_sets_no_limit(self, db, server_mode) -> None:
        await add_connection(db)
        status = await budgets.status_for(db, "admin", {"administrator"}, connection_id=WORK)
        assert status.state == "unlimited"

    async def test_an_unmetered_connection_has_no_limit(self, db, server_mode) -> None:
        await add_connection(db, metered=False)
        await assign(db, role="user", credits=10)
        await spend(db, "u1", 500)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert (status.state, status.used) == ("unlimited", Decimal(500))

    async def test_personal_mode_meters_nothing(self, db, personal_mode) -> None:
        await add_connection(db)
        await assign(db, role="administrator", credits=1)
        await spend(db, "local", 500)
        status = await budgets.status_for(db, "local", {"administrator"}, connection_id=WORK)
        assert status.state == "unlimited"
        assert await budgets.standings(db, "local", {"administrator"}) == []


class TestUsedThisMonth:
    async def test_counts_this_months_credits_on_this_connection(self, db, server_mode) -> None:
        await add_connection(db)
        await add_connection(db, LAB)
        await assign(db, role="user", credits=1000)
        await spend(db, "u1", 300, at=NOV_FIRST)
        await spend(db, "u1", 200, at=NOV_FIRST)
        await spend(db, "u2", 100, at=NOV_FIRST)
        await spend(db, "u1", 400, cid=LAB, at=NOV_FIRST)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert (status.period, status.used) == ("2026-11", Decimal(500))

    async def test_the_allowance_resets_at_the_utc_month_boundary(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await spend(db, "u1", 1000, at=OCT_LAST)
        october = await budgets.status_for(db, "u1", {"user"}, now=OCT_LAST, connection_id=WORK)
        november = await budgets.status_for(db, "u1", {"user"}, now=NOV_FIRST, connection_id=WORK)
        assert october.state == "exceeded"
        assert (november.used, november.state) == (Decimal(0), "ok")

    async def test_a_rate_change_never_rewrites_what_was_charged(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await spend(db, "u1", 100)  # 100 credits at 1,000 tokens a credit
        await conn_repo.save_metering(db, Metering(WORK, metered=True, base_rate_tokens=100))
        await spend(db, "u1", 100)  # the same tokens now cost 1,000 credits
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.used == Decimal(1100)

    async def test_the_models_tier_applies(self, db, server_mode) -> None:
        await add_connection(db)
        await assign(db, role="user", credits=1000)
        await conn_repo.save_model(db, ModelRow(WORK, "big", tier="higher"))
        await conn_repo.save_model(db, ModelRow(WORK, "small", tier="lower"))
        for model in ("big", "small", "m"):
            await usage.record_usage(db, 1, model, 1000, 0, "u1", at=NOV, connection_id=WORK)
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.used == Decimal("3.5")  # 2 + 0.5 + 1


class TestSummary:
    async def test_without_a_connection_the_most_used_metered_one(self, db, server_mode) -> None:
        await add_connection(db)
        await add_connection(db, LAB)
        await add_connection(db, "free", metered=False)
        await assign(db, role="user", credits=1000)
        await assign(db, LAB, role="user", credits=100)
        await assign(db, "free", role="user")
        await spend(db, "u1", 500)
        await spend(db, "u1", 85, cid=LAB)
        await spend(db, "u1", 10**6, cid="free")
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV)
        assert (status.connection_id, status.state) == (LAB, "warning")
        listed = await budgets.standings(db, "u1", {"user"}, now=NOV)
        assert [s.connection_id for s in listed] == [LAB, WORK]

    async def test_no_metered_connection_is_unlimited(self, db, server_mode) -> None:
        status = await budgets.status_for(db, "u1", {"user"}, now=NOV)
        assert (status.state, status.connection_id) == ("unlimited", None)


class TestCheck:
    """The synchronous check a turn makes before it starts."""

    @pytest.fixture()
    def metered(self, sync_db: Any, server_mode: None) -> Any:
        sync_db.call(add_connection)
        sync_db.call(assign, role="user", credits=1000)
        return sync_db

    def test_under_the_allowance_passes(self, metered: Any) -> None:
        metered.call(spend, "u1", 799)
        status = budgets.check(metered, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert status.state == "ok"

    def test_warning_still_passes(self, metered: Any) -> None:
        metered.call(spend, "u1", 800)
        assert (
            budgets.check(metered, "u1", {"user"}, now=NOV, connection_id=WORK).state == "warning"
        )

    def test_at_100_percent_a_new_request_is_refused(self, metered: Any) -> None:
        metered.call(spend, "u1", 1000)
        with pytest.raises(BudgetExceeded) as raised:
            budgets.check(metered, "u1", {"user"}, now=NOV, connection_id=WORK)
        assert raised.value.status.used == Decimal(1000)
        assert "monthly credits on Work Bedrock" in str(raised.value)

    def test_the_model_names_the_connection(self, metered: Any) -> None:
        metered.call(spend, "u1", 1000)
        with pytest.raises(BudgetExceeded):
            budgets.check(metered, "u1", {"user"}, now=NOV, model=f"{WORK}/some-model")

    def test_another_connection_is_not_refused(self, metered: Any) -> None:
        metered.call(add_connection, LAB, metered=False)
        metered.call(spend, "u1", 1000)
        status = budgets.check(metered, "u1", {"user"}, now=NOV, model=f"{LAB}/x")
        assert status.state == "unlimited"

    def test_no_connection_refuses_nothing(self, metered: Any) -> None:
        metered.call(spend, "u1", 1000)
        assert budgets.check(metered, "u1", {"user"}, now=NOV).state == "exceeded"

    def test_the_refusal_is_audited(self, metered: Any, monkeypatch: Any) -> None:
        from spark.identity import audit

        events: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            audit, "submit", lambda action, outcome="success", **f: events.append((action, f))
        )
        metered.call(spend, "u1", 1000)
        with pytest.raises(BudgetExceeded):
            budgets.check(metered, "u1", {"user"}, now=NOV, connection_id=WORK)
        [(action, fields)] = events
        assert action == audit.USAGE_REFUSED
        assert fields["user_guid"] == "u1"
        assert fields["details"] == {
            "connection_id": WORK,
            "used": 1000.0,
            "limit": 1000.0,
            "period": "2026-11",
        }

    def test_personal_mode_never_refuses(self, sync_db: Any, personal_mode: None) -> None:
        sync_db.call(add_connection)
        sync_db.call(spend, "local", 10**6)
        status = budgets.check(sync_db, "local", None, now=NOV, connection_id=WORK)
        assert status.state == "unlimited"


class TestConnectionOf:
    def test_a_reference_names_its_connection(self) -> None:
        assert budgets.connection_of("work/claude") == "work"

    def test_a_service_decides(self) -> None:
        from spark.connections.refs import ModelRef

        class Service:
            ref = ModelRef("lab", "x")

        assert budgets.connection_of("work/claude", Service()) == "lab"

    def test_nothing_to_tell(self) -> None:
        assert budgets.connection_of("") is None
        assert budgets.connection_of("bare-model") is None
