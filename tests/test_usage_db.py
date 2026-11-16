"""The usage repository: per-call rows with their credits, monthly totals and the report."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from spark.database import connections as conn_repo
from spark.database import usage
from spark.database.connections import ModelRow
from tests.credit_helpers import allow, metered

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


class TestCredits:
    """Each call is charged when recorded, at its connection's rates and its model's tier."""

    @staticmethod
    async def _row(db) -> dict:
        return await db.fetch_one(
            "SELECT credits_micro, credit_inputs FROM usage_tracking ORDER BY id DESC LIMIT 1"
        )

    async def test_a_call_with_cache_tokens_at_the_default_rates(self, db) -> None:
        await metered(db, "work", rates={})
        # 500 + 100 x 5 + 10,000 x 0.1 + 2,000 x 1.25 = 4,500 weighted: 4.5 credits.
        await usage.record_usage(
            db,
            1,
            "m",
            500,
            100,
            "u1",
            connection_id="work",
            cache_read_tokens=10_000,
            cache_write_tokens=2_000,
            at=NOV_FIRST,
        )
        row = await self._row(db)
        assert int(row["credits_micro"]) == 4_500_000
        assert json.loads(row["credit_inputs"]) == {
            "metered": True,
            "tier": "standard",
            "multiplier": "1",
            "base_rate_tokens": 1000,
            "weights": {"input": "1", "output": "5", "cache_read": "0.1", "cache_write": "1.25"},
        }
        assert await usage.credits_in_period(db, "u1", "work", "2026-11") == Decimal("4.5")

    @pytest.mark.parametrize(("tier", "micro"), [("lower", 500_000), ("higher", 2_000_000)])
    async def test_the_models_tier(self, db, tier: str, micro: int) -> None:
        await metered(db, "work", rates={})
        await conn_repo.save_model(db, ModelRow("work", "m", tier=tier))
        await usage.record_usage(db, 1, "m", 1000, 0, "u1", connection_id="work")
        row = await self._row(db)
        assert int(row["credits_micro"]) == micro
        assert json.loads(row["credit_inputs"])["tier"] == tier

    async def test_an_unmetered_connection_is_charged_for_the_record(self, db) -> None:
        await metered(db, "work", is_metered=False, rates={})
        await usage.record_usage(db, 1, "m", 1000, 0, "u1", connection_id="work")
        row = await self._row(db)
        assert int(row["credits_micro"]) == 1_000_000
        assert json.loads(row["credit_inputs"])["metered"] is False

    async def test_a_connection_with_no_metering_uses_the_defaults(self, db) -> None:
        await usage.record_usage(db, 1, "m", 2000, 0, "u1", connection_id="nowhere")
        assert int((await self._row(db))["credits_micro"]) == 2_000_000

    async def test_a_call_with_no_connection_costs_nothing(self, db) -> None:
        await usage.record_usage(db, 1, "m", 2000, 0, "u1")
        row = await self._row(db)
        assert (int(row["credits_micro"]), row["credit_inputs"]) == (0, None)

    async def test_credits_are_per_user_connection_and_month(self, db) -> None:
        await metered(db, "work", rates={})
        await metered(db, "lab", rates={})
        for user, cid, at in [
            ("u1", "work", NOV_FIRST),
            ("u1", "work", NOV_FIRST),
            ("u2", "work", NOV_FIRST),
            ("u1", "lab", NOV_FIRST),
            ("u1", "work", OCT_LAST),
        ]:
            await usage.record_usage(db, 1, "m", 1000, 0, user, connection_id=cid, at=at)
        assert await usage.credits_in_period(db, "u1", "work", "2026-11") == Decimal(2)

    async def test_concurrent_calls_lose_no_credits(self, db) -> None:
        await metered(db, "work", rates={})
        await asyncio.gather(
            *(
                usage.record_usage(db, 1, "m", 1000, 0, "u1", connection_id="work", at=NOV_FIRST)
                for _ in range(40)
            )
        )
        assert await usage.credits_in_period(db, "u1", "work", "2026-11") == Decimal(40)


class TestAllowanceRows:
    async def test_only_the_user_and_their_roles(self, db) -> None:
        await metered(db, "work")
        await metered(db, "lab")
        await allow(db, "work", role="user", credits=10)
        await allow(db, "work", role="auditor", credits=20)
        await allow(db, "work", user="u1", credits=None)
        await allow(db, "work", user="u2", credits=5)
        await allow(db, "lab", role="user", credits=99)
        rows = await usage.allowance_rows(db, "work", "u1", {"user"})
        assert sorted((r.role or "", r.user_guid or "", r.monthly_credits) for r in rows) == [
            ("", "u1", None),
            ("user", "", 10),
        ]
        assert await usage.allowance_rows(db, "work", "u3", ()) == []

    async def test_whether_anyone_is_assigned(self, db) -> None:
        await metered(db, "work")
        assert not await usage.connection_has_assignments(db, "work")
        await allow(db, "work", user="u9", credits=1)
        assert await usage.connection_has_assignments(db, "work")


class TestTotals:
    async def test_per_connection_with_the_four_counts_and_credits(self, db) -> None:
        await metered(db, "work", rates={})
        await usage.record_usage(
            db,
            1,
            "m",
            500,
            100,
            "u1",
            connection_id="work",
            cache_read_tokens=10_000,
            cache_write_tokens=2_000,
            at=NOV_FIRST,
        )
        await usage.record_usage(db, 1, "m", 1000, 0, "u2", connection_id="work", at=NOV_FIRST)
        await usage.record_usage(db, 1, "m", 7, 0, "u2", at=NOV_FIRST)
        totals = {
            r["connection_id"]: r for r in await usage.connection_totals(db, period="2026-11")
        }
        assert set(totals) == {"", "work"}
        work = totals["work"]
        assert (
            work["requests"],
            work["input_tokens"],
            work["output_tokens"],
            work["cache_read_tokens"],
            work["cache_write_tokens"],
            work["credits"],
        ) == (2, 1500, 100, 10_000, 2_000, Decimal("5.5"))
        [row] = [
            r for r in await usage.report_all_users(db, period="2026-11") if r["user_guid"] == "u1"
        ]
        assert (row["connection_id"], row["cache_read_tokens"], row["credits"]) == (
            "work",
            10_000,
            Decimal("4.5"),
        )


class TestWardenUsers:
    async def test_linked_guids_and_nothing_created(self, db) -> None:
        from spark.database import user_identities

        await user_identities.link(db, "g1", "w1")
        assert await usage.warden_users_all_users(db) == {"g1": "w1"}
        assert await usage.warden_users_all_users(db) == {"g1": "w1"}
