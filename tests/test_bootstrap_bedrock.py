"""The Amazon Bedrock connection ``spark bootstrap`` adds (task role, no credential).

Three layers: the settings (read and checked by ``collect()``, no database), the step
(``seed_bedrock`` on SQLite and PostgreSQL through the ``spark_db`` fixture), and the
whole run on PostgreSQL (``spark bootstrap`` with a local SMTP server, as
``tests/test_bootstrap.py`` does).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conduit import default_catalogue
from conduit.core.residency import ResidencyScope

from spark.bootstrap import cli
from spark.bootstrap import connections as bc
from spark.bootstrap import settings as bs
from spark.bootstrap.run import Outcome
from spark.connections.discovery import _refusal, offered_in
from spark.connections.model import Connection, residency_from_record, residency_record
from spark.connections.store import DatabaseConnectionStore
from spark.identity import audit
from tests.test_bootstrap_settings import ADMIN, _collect, _complete, _konfig, _problems

REGION = "ap-southeast-2"
AU = "ap-southeast-2,ap-southeast-4"


def _found(**extra: Any) -> bs.BedrockSeed | None:
    return _collect(_konfig(**_complete(**extra))).bedrock


class TestSettings:
    def test_unset_or_empty_turns_the_step_off(self) -> None:
        assert _found() is None
        assert _found(bootstrap__bedrock__region="") is None
        assert _found(bootstrap__bedrock__region="  ") is None

    def test_a_region_alone(self) -> None:
        seed = _found(bootstrap__bedrock__region=REGION)
        assert seed == bs.BedrockSeed(REGION, "Amazon Bedrock (task role)", None)

    def test_a_name_and_residency_regions(self) -> None:
        seed = _found(
            bootstrap__bedrock__region=REGION,
            bootstrap__bedrock__name="Bedrock (Sydney)",
            bootstrap__bedrock__residency_regions=AU,
        )
        assert seed == bs.BedrockSeed(
            REGION, "Bedrock (Sydney)", ("ap-southeast-2", "ap-southeast-4")
        )

    def test_a_json_list_is_accepted(self) -> None:
        seed = _found(
            bootstrap__bedrock__region=REGION,
            bootstrap__bedrock__residency_regions=["ap-southeast-4", REGION],
        )
        assert seed is not None and seed.residency_regions == ("ap-southeast-2", "ap-southeast-4")

    @pytest.mark.parametrize("empty", ["", " ", []])
    def test_an_empty_list_keeps_the_default(self, empty: Any) -> None:
        seed = _found(
            bootstrap__bedrock__region=REGION, bootstrap__bedrock__residency_regions=empty
        )
        assert seed is not None and seed.residency_regions is None

    @pytest.mark.parametrize(
        "region", ["sydney", "AP-SOUTHEAST-2", "ap-southeast", "ap_southeast_2", "x"]
    )
    def test_a_bad_region_is_reported(self, region: str) -> None:
        problems = _problems(_konfig(**_complete(bootstrap__bedrock__region=region)))
        assert len(problems) == 1
        assert "bootstrap.bedrock.region (SPARK__BOOTSTRAP__BEDROCK__REGION)" in problems[0]
        assert f"{region} " not in problems[0] and f"{region!r}" not in problems[0]

    @pytest.mark.parametrize("name", ["x" * 129, "a\nb"])
    def test_a_bad_name_is_reported(self, name: str) -> None:
        problems = _problems(
            _konfig(**_complete(bootstrap__bedrock__region=REGION, bootstrap__bedrock__name=name))
        )
        assert problems == [
            "bootstrap.bedrock.name (SPARK__BOOTSTRAP__BEDROCK__NAME) must be one line of at "
            "most 128 characters"
        ]

    @pytest.mark.parametrize(
        ("listed", "fragment"),
        [
            ("ap-southeast-2,sydney", "must be AWS Regions separated by commas"),
            ("ap-southeast-2,,ap-southeast-4", "must be AWS Regions separated by commas"),
            (",", "must be AWS Regions separated by commas"),
            ("ap-southeast-2,ap-southeast-2", "more than once"),
            ("ap-southeast-4,ap-southeast-6", "must include bootstrap.bedrock.region"),
            (5, "must be a list of AWS Regions"),
        ],
    )
    def test_a_bad_residency_list_is_reported(self, listed: Any, fragment: str) -> None:
        problems = _problems(
            _konfig(
                **_complete(
                    bootstrap__bedrock__region=REGION,
                    bootstrap__bedrock__residency_regions=listed,
                )
            )
        )
        assert len(problems) == 1
        assert (
            "bootstrap.bedrock.residency_regions (SPARK__BOOTSTRAP__BEDROCK__RESIDENCY_REGIONS)"
            in problems[0]
        )
        assert fragment in problems[0]

    def test_every_problem_is_reported_at_once(self) -> None:
        problems = _problems(
            _konfig(
                **_complete(
                    bootstrap__admin__email="not-an-address",
                    bootstrap__bedrock__region="nowhere",
                    bootstrap__bedrock__name="x" * 200,
                )
            )
        )
        text = "\n".join(problems)
        assert "BOOTSTRAP__ADMIN__EMAIL" in text
        assert "BOOTSTRAP__BEDROCK__REGION" in text
        assert "BOOTSTRAP__BEDROCK__NAME" in text

    def test_residency_regions_without_a_region_do_nothing(self) -> None:
        assert _found(bootstrap__bedrock__residency_regions=AU) is None


class TestBuild:
    def test_the_default_residency_is_in_region(self) -> None:
        connection = bc.build(bs.BedrockSeed(REGION))
        assert connection.id == "aws-bedrock" and connection.enabled
        assert connection.service == "bedrock"
        assert dict(connection.settings) == {
            "kind": "bedrock",
            "region": REGION,
            "auth": {"mode": "credential_chain"},
        }
        assert residency_record(connection.residency) == {
            "scopes": ["in_region", "local"],
            "regions": [REGION],
        }

    def test_listed_regions_allow_cross_region_and_never_global(self) -> None:
        connection = bc.build(
            bs.BedrockSeed(REGION, residency_regions=("ap-southeast-2", "ap-southeast-4"))
        )
        assert residency_record(connection.residency) == {
            "scopes": ["cross_region", "in_region", "local"],
            "regions": ["ap-southeast-2", "ap-southeast-4"],
        }
        assert ResidencyScope.GLOBAL not in connection.residency.scopes

    def test_the_au_profiles_over_two_regions_pass_and_the_older_ones_are_refused(self) -> None:
        connection = bc.build(
            bs.BedrockSeed(REGION, residency_regions=("ap-southeast-2", "ap-southeast-4"))
        )
        # What the stored record reads back as, which is what conduit checks.
        policy = residency_from_record(residency_record(connection.residency))
        entries = [
            e
            for e in default_catalogue()
            if e.model_id.startswith("au.anthropic.") and e.kind == "chat"
        ]
        assert entries
        for entry in entries:
            assert offered_in(entry, REGION)
            refused = _refusal(policy, entry, connection)
            if "ap-southeast-6" in entry.regions:
                assert refused is not None and "ap-southeast-6" in refused
            else:
                assert refused is None, entry.model_id

    def test_the_default_residency_refuses_every_au_profile(self) -> None:
        connection = bc.build(bs.BedrockSeed(REGION))
        entries = [e for e in default_catalogue() if e.model_id.startswith("au.anthropic.")]
        assert entries
        assert all(_refusal(connection.residency, e, connection) for e in entries)


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def emit(action: str, outcome: str = "success", **fields: Any) -> None:
        events.append({"action": action, **fields})

    monkeypatch.setattr(audit, "emit", emit)
    return events


async def _store_one(db: Any, **changes: Any) -> Connection:
    store = DatabaseConnectionStore(db)
    base = bc.build(bs.BedrockSeed("us-east-1"))
    fields = {
        "id": base.id,
        "name": "Administrator's own",
        "settings": base.settings,
        "residency": base.residency,
        "enabled": base.enabled,
        **changes,
    }
    connection = Connection(**fields)
    await store.save_connection(connection)
    return connection


async def _marked(db: Any) -> bool:
    return (
        await db.fetch_value("SELECT 1 FROM instance_settings WHERE key = ?", (bc.MARKER,))
        is not None
    )


class TestSeed:
    async def test_created_on_a_fresh_database(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        result = await bc.seed_bedrock(spark_db, bs.BedrockSeed(REGION), actor="spark-bootstrap")
        assert result == "created"
        stored = await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock")
        assert stored is not None
        assert dict(stored.settings) == {
            "kind": "bedrock",
            "region": REGION,
            "auth": {"mode": "credential_chain"},
        }
        assert "secret://" not in json.dumps(dict(stored.settings))
        assert stored.name == "Amazon Bedrock (task role)" and stored.enabled
        assert residency_record(stored.residency) == {
            "scopes": ["in_region", "local"],
            "regions": [REGION],
        }
        assert await _marked(spark_db)
        assert len(audited) == 1
        event = audited[0]
        assert event["action"] == audit.SETTING_CHANGED
        assert event["actor"] == "spark-bootstrap"
        assert (event["resource_type"], event["resource_id"]) == ("connection", "aws-bedrock")
        assert event["details"]["change"] == "created"
        assert "region" in event["details"]["fields"]
        assert REGION not in json.dumps(event)

    async def test_residency_regions_are_stored(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        seed = bs.BedrockSeed(REGION, residency_regions=("ap-southeast-2", "ap-southeast-4"))
        await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap")
        stored = await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock")
        assert stored is not None
        assert residency_record(stored.residency) == {
            "scopes": ["cross_region", "in_region", "local"],
            "regions": ["ap-southeast-2", "ap-southeast-4"],
        }

    async def test_a_second_run_changes_nothing(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        seed = bs.BedrockSeed(REGION)
        await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap")
        before = await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock")
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "exists"
        assert await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock") == before
        assert len(audited) == 1

    @pytest.mark.parametrize(
        "changes",
        [
            {"enabled": False},
            {"name": "Renamed by an administrator"},
            {
                "settings": {
                    "kind": "bedrock",
                    "region": "eu-west-1",
                    "auth": {"mode": "credential_chain"},
                }
            },
        ],
        ids=["disabled", "edited", "another-region"],
    )
    async def test_an_existing_connection_is_left_alone(
        self, spark_db: Any, audited: list[dict[str, Any]], changes: dict[str, Any]
    ) -> None:
        existing = await _store_one(spark_db, **changes)
        result = await bc.seed_bedrock(
            spark_db,
            bs.BedrockSeed(REGION, residency_regions=("ap-southeast-2", "ap-southeast-4")),
            actor="spark-bootstrap",
        )
        assert result == "exists"
        assert await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock") == existing
        assert audited == []

    async def test_a_deleted_connection_is_not_added_again(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        seed = bs.BedrockSeed(REGION)
        await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap")
        assert await DatabaseConnectionStore(spark_db).delete_connection("aws-bedrock")
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "declined"
        assert await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock") is None
        assert len(audited) == 1

    async def test_an_administrators_connection_that_is_later_deleted_stays_deleted(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        await _store_one(spark_db)
        seed = bs.BedrockSeed(REGION)
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "exists"
        await DatabaseConnectionStore(spark_db).delete_connection("aws-bedrock")
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "declined"

    async def test_conduit_refusing_the_settings_writes_nothing(
        self, spark_db: Any, audited: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark.connections.model import ConnectionInvalid

        def refuse(connection: Connection, registry: Any) -> None:
            raise ConnectionInvalid("refused")

        monkeypatch.setattr(bc, "validate", refuse)
        with pytest.raises(ConnectionInvalid):
            await bc.seed_bedrock(spark_db, bs.BedrockSeed(REGION), actor="spark-bootstrap")
        assert await DatabaseConnectionStore(spark_db).get_connection("aws-bedrock") is None
        assert not await _marked(spark_db)
        assert audited == []


class TestDescribe:
    @pytest.mark.parametrize(
        ("result", "text"),
        [
            ("created", "aws-bedrock added"),
            ("exists", "already exists, left unchanged"),
            ("declined", "deleted by an administrator, not added again"),
        ],
    )
    def test_each_result_has_a_line(self, result: str, text: str) -> None:
        described = cli.describe(Outcome({}, (), False, False, True, result))
        assert text in described
        assert ADMIN not in described

    def test_off_says_nothing(self) -> None:
        assert "bedrock" not in cli.describe(Outcome({}, (), False, False, True))
