"""Connection repositories on SQLite and PostgreSQL: connections, models, overlays,
metering and assignments, with the secret-reference rule and the cascade on delete."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spark.database import connections as repo

pytestmark = pytest.mark.asyncio

KEY_REF = "secret://connections.work.api_key"


def _connection(cid: str = "work", **settings: Any) -> repo.ConnectionRow:
    body = {"kind": "anthropic", "api_key": KEY_REF, **settings}
    return repo.ConnectionRow(
        id=cid,
        name="Work Claude",
        kind="anthropic",
        settings=body,
        residency={"scopes": ["global"], "regions": None},
    )


class TestConnections:
    async def test_round_trip_and_replace(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        stored = await repo.get_connection(spark_db, "work")
        assert stored == _connection()
        await repo.save_connection(
            spark_db, repo.ConnectionRow("work", "Renamed", "ollama", {"kind": "ollama"}, {})
        )
        listed = await repo.list_connections(spark_db)
        assert [(c.id, c.name, c.kind) for c in listed] == [("work", "Renamed", "ollama")]

    async def test_two_connections_of_one_kind(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection("work"))
        await repo.save_connection(
            spark_db, _connection("home", api_key="secret://connections.home.api_key")
        )
        assert [c.id for c in await repo.list_connections(spark_db)] == ["home", "work"]

    @pytest.mark.parametrize(
        "settings",
        [
            {"api_key": "sk-example-not-real"},
            {"auth": {"mode": "access_keys", "secret_access_key": "plain-example"}},
            {"service": {"name": "openai", "api_key": "sk-example-not-real"}},
        ],
    )
    async def test_refuses_a_plain_text_credential(
        self, spark_db: Any, settings: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError, match="secret:// reference") as refused:
            await repo.save_connection(spark_db, _connection(**settings))
        assert "example" not in str(refused.value)
        assert await repo.list_connections(spark_db) == []

    async def test_kind_must_match_settings(self, spark_db: Any) -> None:
        row = repo.ConnectionRow("work", "W", "gemini", {"kind": "anthropic"}, {})
        with pytest.raises(ValueError, match="kind"):
            await repo.save_connection(spark_db, row)

    async def test_delete_takes_everything_with_it(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        await repo.save_model(spark_db, repo.ModelRow("work", "claude-sonnet-5", tier="higher"))
        await repo.save_metering(spark_db, repo.Metering("work", metered=True))
        await repo.save_assignment(spark_db, repo.Assignment("work", role="staff"))
        assert await repo.delete_connection(spark_db, "work") is True
        assert await repo.get_connection(spark_db, "work") is None
        assert await repo.list_models(spark_db, "work") == []
        assert await repo.assignments(spark_db, "work") == []
        assert await repo.get_metering(spark_db, "work") == repo.Metering("work")
        assert await repo.delete_connection(spark_db, "work") is False


class TestModels:
    async def test_tiers_and_enablement(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        await repo.save_model(spark_db, repo.ModelRow("work", "b-model"))
        await repo.save_model(spark_db, repo.ModelRow("work", "a-model", False, "lower"))
        assert await repo.list_models(spark_db, "work") == [
            repo.ModelRow("work", "a-model", False, "lower"),
            repo.ModelRow("work", "b-model", True, "standard"),
        ]
        await repo.save_model(spark_db, repo.ModelRow("work", "a-model", True, "higher"))
        assert (await repo.list_models(spark_db))[0].tier == "higher"
        assert await repo.delete_model(spark_db, "work", "a-model") is True

    async def test_unknown_tier_refused(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        with pytest.raises(ValueError, match="tier"):
            await repo.save_model(spark_db, repo.ModelRow("work", "m", True, "premium"))  # type: ignore[arg-type]


class TestOverlays:
    async def test_round_trip(self, spark_db: Any) -> None:
        entry = repo.OverlayRow(
            provider="ollama",
            model_id="mistral-small:24b",
            context_window=32_768,
            output_limit=8_192,
            residency_scope="local",
            tools=True,
        )
        regional = repo.OverlayRow(
            provider="openai",
            model_id="example-gpt5-swedencentral",
            context_window=400_000,
            output_limit=128_000,
            residency_scope="in_region",
            regions=("swedencentral",),
        )
        await repo.save_overlay(spark_db, entry)
        await repo.save_overlay(spark_db, regional)
        assert await repo.list_overlays(spark_db) == [entry, regional]
        assert await repo.delete_overlay(spark_db, "ollama", "mistral-small:24b") is True
        assert await repo.list_overlays(spark_db) == [regional]

    async def test_bad_scope_refused(self, spark_db: Any) -> None:
        with pytest.raises(ValueError, match="residency scope"):
            await repo.save_overlay(
                spark_db, repo.OverlayRow("ollama", "m", 1, "everywhere", output_limit=1)
            )


class TestMetering:
    async def test_defaults_until_saved(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        default = await repo.get_metering(spark_db, "work")
        assert (
            default.input_weight,
            default.output_weight,
            default.cache_read_weight,
            default.cache_write_weight,
        ) == (1.0, 5.0, 0.1, 1.25)
        assert default.metered is False

    async def test_round_trip(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        saved = repo.Metering("work", True, 2000, 3.0, 0.25, 1.0, 4.0, 0.2, 1.5)
        await repo.save_metering(spark_db, saved)
        assert await repo.get_metering(spark_db, "work") == saved

    @pytest.mark.parametrize(
        "bad",
        [
            {"base_rate_tokens": 0},
            {"higher_multiplier": 0.0},
            {"lower_multiplier": -1.0},
            {"output_weight": -0.5},
        ],
    )
    async def test_bad_values_refused(self, spark_db: Any, bad: dict[str, Any]) -> None:
        await repo.save_connection(spark_db, _connection())
        with pytest.raises(ValueError):
            await repo.save_metering(spark_db, repo.Metering("work", **bad))


class TestAssignments:
    async def test_user_and_roles(self, spark_db: Any) -> None:
        for cid in ("work", "home", "lab"):
            await repo.save_connection(spark_db, _connection(cid))
        await repo.save_assignment(spark_db, repo.Assignment("work", role="staff"))
        await repo.save_assignment(spark_db, repo.Assignment("home", user_guid="u1"))
        await repo.save_assignment(spark_db, repo.Assignment("lab", role="research"))
        assert await repo.connections_for(spark_db, "u1", ["staff"]) == {"work", "home"}
        assert await repo.connections_for(spark_db, "u2", ["staff", "research"]) == {
            "work",
            "lab",
        }
        assert await repo.connections_for(spark_db, "u2", []) == set()

    async def test_saving_again_replaces_the_allowance(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        await repo.save_assignment(
            spark_db, repo.Assignment("work", role="staff", monthly_credits=5)
        )
        await repo.save_assignment(
            spark_db, repo.Assignment("work", role="staff", monthly_credits=50)
        )
        assert await repo.assignments(spark_db, "work") == [
            repo.Assignment("work", role="staff", monthly_credits=50)
        ]
        assert await repo.delete_assignment(spark_db, "work", role="staff") is True
        assert await repo.assignments(spark_db) == []

    @pytest.mark.parametrize(
        "assignment",
        [
            repo.Assignment("work"),
            repo.Assignment("work", role="staff", user_guid="u1"),
            repo.Assignment("work", role="staff", monthly_credits=-1),
        ],
    )
    async def test_malformed_refused(self, spark_db: Any, assignment: repo.Assignment) -> None:
        await repo.save_connection(spark_db, _connection())
        with pytest.raises(ValueError):
            await repo.save_assignment(spark_db, assignment)

    async def test_parallel_assignments_keep_one_row_each(self, spark_db: Any) -> None:
        await repo.save_connection(spark_db, _connection())
        await asyncio.gather(
            *(
                repo.save_assignment(spark_db, repo.Assignment("work", user_guid=f"u{i}"))
                for i in range(8)
            )
        )
        rows = await repo.assignments(spark_db, "work")
        assert sorted(r.user_guid or "" for r in rows) == [f"u{i}" for i in range(8)]


@pytest.mark.postgres
class TestNotify:
    async def test_writes_notify_other_replicas(
        self, spark_db: Any, spark_db_settings: Any
    ) -> None:
        from spark.mode import Dialect

        if spark_db.dialect is not Dialect.POSTGRESQL:
            pytest.skip("PostgreSQL only")
        import psycopg

        listener = await psycopg.AsyncConnection.connect(spark_db_settings.url, autocommit=True)
        try:
            await listener.execute("LISTEN spark_instance_settings")
            await repo.save_connection(spark_db, _connection())
            gen = listener.notifies(timeout=5)
            notice = await anext(gen)
            assert notice.payload == repo.NOTICE
        finally:
            await listener.close()
