"""The mapping of SPARK user guids onto warden users (SQLite and PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pytest

from spark.database import user_identities
from spark.database.user_identities import IdentityConflict

WARDEN_A = "0b6f3c1e-8a52-4d0e-9a7f-000000000001"
WARDEN_B = "0b6f3c1e-8a52-4d0e-9a7f-000000000002"
INTERIM = "user-interim-0001"


class TestUserGuidFor:
    async def test_first_use_maps_the_warden_user_to_its_own_id(self, spark_db: Any) -> None:
        assert await user_identities.user_guid_for(spark_db, WARDEN_A) == WARDEN_A
        assert await user_identities.warden_user_for(spark_db, WARDEN_A) == WARDEN_A

    async def test_is_stable_across_calls(self, spark_db: Any) -> None:
        first = await user_identities.user_guid_for(spark_db, WARDEN_A)
        second = await user_identities.user_guid_for(spark_db, WARDEN_A)
        assert first == second
        count = await spark_db.fetch_value("SELECT COUNT(*) FROM user_identities")
        assert count == 1

    async def test_returns_a_linked_guid(self, spark_db: Any) -> None:
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        assert await user_identities.user_guid_for(spark_db, WARDEN_A) == INTERIM

    async def test_a_second_warden_user_never_gets_the_first_users_guid(
        self, spark_db: Any
    ) -> None:
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        first = await user_identities.user_guid_for(spark_db, WARDEN_A)
        second = await user_identities.user_guid_for(spark_db, WARDEN_B)
        assert first == INTERIM
        assert second != first
        assert await user_identities.warden_user_for(spark_db, second) == WARDEN_B

    async def test_a_guid_already_taken_is_not_handed_to_another_warden_user(
        self, spark_db: Any
    ) -> None:
        # WARDEN_B's own id is already WARDEN_A's guid: B must get a different one.
        await user_identities.link(spark_db, WARDEN_B, WARDEN_A)
        guid = await user_identities.user_guid_for(spark_db, WARDEN_B)
        assert guid != WARDEN_B
        assert await user_identities.user_guid_for(spark_db, WARDEN_B) == guid
        assert await user_identities.warden_user_for(spark_db, WARDEN_B) == WARDEN_A
        assert await user_identities.warden_user_for(spark_db, guid) == WARDEN_B

    async def test_joins_a_callers_transaction(self, spark_db: Any) -> None:
        with pytest.raises(RuntimeError):
            async with spark_db.transaction() as conn:
                await user_identities.user_guid_for(conn, WARDEN_A)
                raise RuntimeError("roll back")
        assert await user_identities.warden_user_for(spark_db, WARDEN_A) is None


class TestWardenUserFor:
    async def test_unknown_guid(self, spark_db: Any) -> None:
        assert await user_identities.warden_user_for(spark_db, INTERIM) is None


class TestLink:
    async def test_binds_an_existing_guid(self, spark_db: Any) -> None:
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        assert await user_identities.warden_user_for(spark_db, INTERIM) == WARDEN_A

    async def test_linking_the_same_pair_again_is_a_no_op(self, spark_db: Any) -> None:
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        assert await user_identities.warden_user_for(spark_db, INTERIM) == WARDEN_A

    async def test_refuses_to_rebind_the_guid(self, spark_db: Any) -> None:
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        with pytest.raises(IdentityConflict):
            await user_identities.link(spark_db, INTERIM, WARDEN_B)
        assert await user_identities.warden_user_for(spark_db, INTERIM) == WARDEN_A
        assert await user_identities.user_guid_for(spark_db, WARDEN_B) == WARDEN_B

    async def test_refuses_to_rebind_the_warden_user(self, spark_db: Any) -> None:
        await user_identities.link(spark_db, INTERIM, WARDEN_A)
        with pytest.raises(IdentityConflict):
            await user_identities.link(spark_db, "user-other-0002", WARDEN_A)
        assert await user_identities.user_guid_for(spark_db, WARDEN_A) == INTERIM
        assert await user_identities.warden_user_for(spark_db, "user-other-0002") is None

    async def test_refuses_a_warden_user_already_mapped_on_first_use(self, spark_db: Any) -> None:
        await user_identities.user_guid_for(spark_db, WARDEN_A)
        with pytest.raises(IdentityConflict):
            await user_identities.link(spark_db, INTERIM, WARDEN_A)
