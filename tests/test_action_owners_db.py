"""Actions across users: the worker's unscoped listing, refused runs and per-user names.

Each test runs on SQLite and (marked postgres) PostgreSQL through ``spark_db``.
"""

from __future__ import annotations

import pytest

from spark.database import autonomous_actions
from spark.database.autonomous_actions import DuplicateActionName
from spark.database.scope import NotOwned
from spark.db import Database

ADA = "user-ada-example"
GRACE = "user-grace-example"


class TestWorkerListing:
    async def test_lists_every_users_enabled_actions_with_their_owner(
        self, spark_db: Database
    ) -> None:
        ada = await autonomous_actions.create_action(spark_db, "ada-nightly", "p", "m", ADA)
        grace = await autonomous_actions.create_action(spark_db, "grace-daily", "p", "m", GRACE)
        off = await autonomous_actions.create_action(spark_db, "grace-off", "p", "m", GRACE)
        await autonomous_actions.update_action(spark_db, off, GRACE, is_enabled=0)
        rows = await autonomous_actions.get_enabled_actions_for_worker(spark_db)
        assert {(r["id"], r["user_guid"]) for r in rows} == {(ada, ADA), (grace, GRACE)}

    async def test_the_per_user_listing_is_unchanged(self, spark_db: Database) -> None:
        await autonomous_actions.create_action(spark_db, "ada-nightly", "p", "m", ADA)
        mine = await autonomous_actions.create_action(spark_db, "grace-daily", "p", "m", GRACE)
        rows = await autonomous_actions.get_enabled_actions(spark_db, GRACE)
        assert [r["id"] for r in rows] == [mine]


class TestRefusedRun:
    async def test_records_a_refused_run_for_the_owner(self, spark_db: Database) -> None:
        aid = await autonomous_actions.create_action(spark_db, "ada-nightly", "p", "m", ADA)
        run_id = await autonomous_actions.record_refused_run(
            spark_db, aid, ADA, "the owner does not hold spark.actions"
        )
        runs = await autonomous_actions.get_action_runs(spark_db, ADA, aid)
        assert [(r["id"], r["status"], r["error_message"]) for r in runs] == [
            (run_id, "refused", "the owner does not hold spark.actions")
        ]
        assert runs[0]["completed_at"] is not None
        assert runs[0]["user_guid"] == ADA
        action = await autonomous_actions.get_action(spark_db, aid, ADA)
        assert action is not None
        assert action["last_run_at"] is None  # a refusal is not a run
        assert action["is_enabled"] == 1

    async def test_never_under_another_users_action(self, spark_db: Database) -> None:
        aid = await autonomous_actions.create_action(spark_db, "ada-nightly", "p", "m", ADA)
        with pytest.raises(NotOwned):
            await autonomous_actions.record_refused_run(spark_db, aid, GRACE, "no")
        assert await autonomous_actions.get_action_runs(spark_db, ADA, aid) == []


class TestNamesPerUser:
    async def test_two_users_may_share_a_name(self, spark_db: Database) -> None:
        await autonomous_actions.create_action(spark_db, "nightly", "p", "m", ADA)
        mine = await autonomous_actions.create_action(spark_db, "nightly", "p", "m", GRACE)
        assert [a["id"] for a in await autonomous_actions.get_all_actions(spark_db, GRACE)] == [
            mine
        ]

    async def test_one_user_may_not_repeat_a_name(self, spark_db: Database) -> None:
        await autonomous_actions.create_action(spark_db, "nightly", "p", "m", GRACE)
        with pytest.raises(DuplicateActionName) as exc:
            await autonomous_actions.create_action(spark_db, "nightly", "p", "m", GRACE)
        assert "nightly" in str(exc.value)
        assert GRACE not in str(exc.value)

    async def test_renaming_to_another_users_name_is_allowed(self, spark_db: Database) -> None:
        await autonomous_actions.create_action(spark_db, "ada-nightly", "p", "m", ADA)
        mine = await autonomous_actions.create_action(spark_db, "grace-daily", "p", "m", GRACE)
        await autonomous_actions.update_action(spark_db, mine, GRACE, name="ada-nightly")
        action = await autonomous_actions.get_action(spark_db, mine, GRACE)
        assert action is not None and action["name"] == "ada-nightly"

    async def test_renaming_to_ones_own_other_name_is_refused(self, spark_db: Database) -> None:
        await autonomous_actions.create_action(spark_db, "first", "p", "m", GRACE)
        second = await autonomous_actions.create_action(spark_db, "second", "p", "m", GRACE)
        with pytest.raises(DuplicateActionName):
            await autonomous_actions.update_action(spark_db, second, GRACE, name="first")
        # Keeping its own name is not a clash.
        await autonomous_actions.update_action(spark_db, second, GRACE, name="second")

    async def test_the_database_refuses_a_repeat_for_one_user(self, spark_db: Database) -> None:
        """The constraint itself is per user, whatever the repository checks first."""
        insert = (
            "INSERT INTO autonomous_actions (name, action_prompt, model_id, user_guid) "
            "VALUES (?, 'p', 'm', ?)"
        )
        await spark_db.execute(insert, ("nightly", ADA))
        await spark_db.execute(insert, ("nightly", GRACE))
        with pytest.raises(Exception, match="(?i)unique"):
            await spark_db.execute(insert, ("nightly", GRACE))
