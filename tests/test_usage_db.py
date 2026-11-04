"""The usage repository: per-call rows, monthly totals, the report and the budgets (A14)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from spark.database import usage

OCT_LAST = datetime(2026, 10, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
NOV_FIRST = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(spark_db):
    """A migrated SPARK database (SQLite, and PostgreSQL when marked)."""
    return spark_db


class TestPeriod:
    def test_the_period_is_the_utc_month(self) -> None:
        assert usage.period_of(OCT_LAST) == "2026-10"
        assert usage.period_of(NOV_FIRST) == "2026-11"

    def test_a_local_time_is_converted_to_utc_first(self) -> None:
        # 09:00 on 1 November in UTC+10 is still 31 October in UTC.
        brisbane = timezone(timedelta(hours=10))
        assert usage.period_of(datetime(2026, 11, 1, 9, 0, tzinfo=brisbane)) == "2026-10"

    def test_a_naive_time_is_taken_as_utc(self) -> None:
        assert usage.period_of(datetime(2026, 11, 1, 0, 0)) == "2026-11"

    def test_next_period_start(self) -> None:
        assert usage.period_start("2026-11") == NOV_FIRST
        assert usage.next_period_start("2026-12") == datetime(2027, 1, 1, tzinfo=timezone.utc)


class TestRecording:
    async def test_a_call_counts_input_and_output_in_its_month(self, db) -> None:
        await usage.record_usage(db, 7, "m1", 100, 40, "u1", at=NOV_FIRST)
        assert await usage.tokens_in_period(db, "u1", "2026-11") == 140

    async def test_the_monthly_boundary_is_utc(self, db) -> None:
        await usage.record_usage(db, 7, "m1", 100, 0, "u1", at=OCT_LAST)
        await usage.record_usage(db, 7, "m1", 5, 0, "u1", at=NOV_FIRST)
        assert await usage.tokens_in_period(db, "u1", "2026-10") == 100
        assert await usage.tokens_in_period(db, "u1", "2026-11") == 5

    async def test_totals_are_per_user(self, db) -> None:
        await usage.record_usage(db, 1, "m1", 10, 10, "u1", at=NOV_FIRST)
        await usage.record_usage(db, 2, "m1", 999, 1, "u2", at=NOV_FIRST)
        assert await usage.tokens_in_period(db, "u1", "2026-11") == 20

    async def test_no_usage_is_zero(self, db) -> None:
        assert await usage.tokens_in_period(db, "nobody", "2026-11") == 0

    async def test_an_action_call_has_no_conversation(self, db) -> None:
        await usage.record_usage(
            db, None, "m1", 3, 4, "owner", source="action", action_id=12, at=NOV_FIRST
        )
        [row] = await usage.report_all_users(db, period="2026-11")
        assert row["user_guid"] == "owner"
        assert await usage.tokens_in_period(db, "owner", "2026-11") == 7

    async def test_an_unknown_source_is_refused(self, db) -> None:
        with pytest.raises(ValueError, match="source"):
            await usage.record_usage(db, 1, "m1", 1, 1, "u1", source="billing")

    async def test_negative_counts_are_refused(self, db) -> None:
        with pytest.raises(ValueError, match="token"):
            await usage.record_usage(db, 1, "m1", -1, 1, "u1")

    async def test_concurrent_calls_lose_nothing(self, db) -> None:
        # Fifty calls for one user at once: inserts only, so the sum is exact.
        await asyncio.gather(
            *(
                usage.record_usage(db, 1, "m1", i, 1, "u1", source="chat", at=NOV_FIRST)
                for i in range(50)
            )
        )
        assert await usage.tokens_in_period(db, "u1", "2026-11") == sum(range(50)) + 50


class TestReport:
    async def test_grouped_by_month_user_and_model(self, db) -> None:
        await usage.record_usage(db, 1, "m1", 10, 1, "u1", at=OCT_LAST)
        await usage.record_usage(db, 1, "m1", 20, 2, "u1", at=NOV_FIRST)
        await usage.record_usage(db, 1, "m1", 30, 3, "u1", at=NOV_FIRST)
        await usage.record_usage(db, 1, "m2", 40, 4, "u1", at=NOV_FIRST)
        await usage.record_usage(db, 2, "m1", 50, 5, "u2", at=NOV_FIRST)

        rows = await usage.report_all_users(db)
        keyed = {
            (r["period"], r["user_guid"], r["model_id"]): (
                r["requests"],
                r["input_tokens"],
                r["output_tokens"],
            )
            for r in rows
        }
        assert keyed == {
            ("2026-10", "u1", "m1"): (1, 10, 1),
            ("2026-11", "u1", "m1"): (2, 50, 5),
            ("2026-11", "u1", "m2"): (1, 40, 4),
            ("2026-11", "u2", "m1"): (1, 50, 5),
        }
        # Newest month first.
        assert rows[0]["period"] == "2026-11"

    async def test_one_month(self, db) -> None:
        await usage.record_usage(db, 1, "m1", 10, 1, "u1", at=OCT_LAST)
        await usage.record_usage(db, 1, "m1", 20, 2, "u1", at=NOV_FIRST)
        rows = await usage.report_all_users(db, period="2026-10")
        assert [(r["period"], r["input_tokens"]) for r in rows] == [("2026-10", 10)]

    async def test_periods_newest_first(self, db) -> None:
        await usage.record_usage(db, 1, "m1", 1, 1, "u1", at=OCT_LAST)
        await usage.record_usage(db, 1, "m1", 1, 1, "u2", at=NOV_FIRST)
        assert await usage.periods_all_users(db) == ["2026-11", "2026-10"]


class TestRoleBudgets:
    async def test_none_set(self, db) -> None:
        assert await usage.role_budgets(db) == {}

    async def test_set_replace_and_clear(self, db) -> None:
        await usage.set_role_budget(db, "user", 1000)
        await usage.set_role_budget(db, "power_user", 5000)
        await usage.set_role_budget(db, "user", 2000)
        assert await usage.role_budgets(db) == {"user": 2000, "power_user": 5000}
        await usage.clear_role_budget(db, "user")
        assert await usage.role_budgets(db) == {"power_user": 5000}

    @pytest.mark.parametrize("tokens", [0, -5])
    async def test_a_budget_must_be_positive(self, db, tokens: int) -> None:
        with pytest.raises(ValueError, match="positive"):
            await usage.set_role_budget(db, "user", tokens)

    async def test_large_budgets_fit(self, db) -> None:
        await usage.set_role_budget(db, "user", 10_000_000_000)
        assert await usage.role_budgets(db) == {"user": 10_000_000_000}


class TestUserOverrides:
    async def test_no_override(self, db) -> None:
        assert await usage.user_override(db, "u1") is None

    async def test_a_number(self, db) -> None:
        await usage.set_user_override(db, "u1", 300)
        override = await usage.user_override(db, "u1")
        assert override is not None and override.monthly_tokens == 300

    async def test_explicitly_unlimited(self, db) -> None:
        await usage.set_user_override(db, "u1", None)
        override = await usage.user_override(db, "u1")
        assert override is not None and override.monthly_tokens is None

    async def test_per_user(self, db) -> None:
        await usage.set_user_override(db, "u1", 300)
        assert await usage.user_override(db, "u2") is None

    async def test_replace_and_clear(self, db) -> None:
        await usage.set_user_override(db, "u1", 300)
        await usage.set_user_override(db, "u1", None)
        assert await usage.user_overrides_all_users(db) == {"u1": None}
        await usage.clear_user_override(db, "u1")
        assert await usage.user_override(db, "u1") is None
        assert await usage.user_overrides_all_users(db) == {}

    async def test_a_number_must_be_positive(self, db) -> None:
        with pytest.raises(ValueError, match="positive"):
            await usage.set_user_override(db, "u1", 0)


class TestWardenUsers:
    async def test_linked_guids_and_nothing_created(self, db) -> None:
        from spark.database import user_identities

        await user_identities.link(db, "g1", "w1")
        assert await usage.warden_users_all_users(db) == {"g1": "w1"}
        assert await usage.warden_users_all_users(db) == {"g1": "w1"}
