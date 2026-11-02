"""Action names are unique per user, not across users.

Each test runs on SQLite and (marked postgres) PostgreSQL through ``spark_db``.
"""

from __future__ import annotations

import pytest

from spark.database import autonomous_actions
from spark.database.autonomous_actions import DuplicateActionName
from spark.db import Database

ADA = "user-ada-example"
GRACE = "user-grace-example"


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
