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
from conduit import (
    DiscoveredModel,
    DiscoveryOutcome,
    LiteralSecretResolver,
    ProviderRegistry,
    default_catalogue,
    join_discovered,
)
from conduit.core.residency import ResidencyScope

from spark.bootstrap import cli
from spark.bootstrap import connections as bc
from spark.bootstrap import settings as bs
from spark.bootstrap.run import Outcome
from spark.connections.model import (
    Connection,
    ConnectionInvalid,
    residency_from_record,
    residency_record,
    validate,
)
from spark.connections.runtime import production_factories
from spark.connections.store import DatabaseConnectionStore
from spark.identity import audit
from tests.test_bootstrap_settings import ADMIN, _collect, _complete, _konfig, _problems

REGION = "ap-southeast-2"
AU = "ap-southeast-2,ap-southeast-4"
MODELS = (
    "amazon.nova-micro-v1:0,amazon.nova-lite-v1:0,amazon.nova-pro-v1:0,"
    "au.anthropic.claude-sonnet-4-6,au.anthropic.claude-haiku-4-5-20251001-v1:0,"
    "au.anthropic.claude-opus-4-6-v1"
)


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

    def test_models_are_read_as_text_or_a_list(self) -> None:
        text = _found(bootstrap__bedrock__region=REGION, bootstrap__bedrock__models=MODELS)
        listed = _found(
            bootstrap__bedrock__region=REGION, bootstrap__bedrock__models=MODELS.split(",")
        )
        assert text is not None and text.models == tuple(MODELS.split(","))
        assert listed == text

    @pytest.mark.parametrize("empty", ["", " ", []])
    def test_no_models_leave_the_connection_uncurated(self, empty: Any) -> None:
        seed = _found(bootstrap__bedrock__region=REGION, bootstrap__bedrock__models=empty)
        assert seed is not None and seed.models is None
        seed = _found(bootstrap__bedrock__region=REGION)
        assert seed is not None and seed.models is None

    @pytest.mark.parametrize(
        ("listed", "fragment"),
        [
            ("amazon.nova-lite-v1:0,not a model", "must be model ids separated by commas"),
            ("amazon.nova-lite-v1:0,,au.x", "must be model ids separated by commas"),
            ("a/b", "must be model ids separated by commas"),
            ("a.b,a.b", "more than once"),
            (5, "must be a list of model ids"),
        ],
    )
    def test_a_bad_model_list_is_reported_without_its_value(
        self, listed: Any, fragment: str
    ) -> None:
        problems = _problems(
            _konfig(
                **_complete(bootstrap__bedrock__region=REGION, bootstrap__bedrock__models=listed)
            )
        )
        assert len(problems) == 1
        assert "bootstrap.bedrock.models (SPARK__BOOTSTRAP__BEDROCK__MODELS)" in problems[0]
        assert fragment in problems[0]
        assert "not a model" not in problems[0]

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

    @staticmethod
    def _joined(seed: bs.BedrockSeed) -> dict[str, Any]:
        """The AU profiles as a listing reports them, joined as conduit 0.1.0 does (the
        default catalogue carries no Region data: the listing is the source of truth)."""
        connection = bc.build(seed)
        policy = residency_from_record(residency_record(connection.residency))
        validated = ProviderRegistry(
            secrets=LiteralSecretResolver({}), factories=production_factories()
        ).validate(connection.id, connection.settings)
        listed = [
            DiscoveredModel(
                "au.anthropic.claude-new-v1:0",
                scope=ResidencyScope.CROSS_REGION,
                regions=frozenset({"ap-southeast-2", "ap-southeast-4"}),
                base_model_id="anthropic.claude-new-v1:0",
            ),
            DiscoveredModel(
                "au.anthropic.claude-old-v1:0",
                scope=ResidencyScope.CROSS_REGION,
                regions=frozenset({"ap-southeast-2", "ap-southeast-6"}),
                base_model_id="anthropic.claude-old-v1:0",
            ),
        ]
        models, _ = join_discovered(
            listed,
            default_catalogue(),
            catalogue_provider="bedrock",
            region=validated.region,
            residency=policy,
            provider=connection.id,
            connection=validated,
        )
        return {m.model_id: m for m in models}

    def test_the_au_profiles_over_two_regions_pass_and_the_older_ones_are_refused(self) -> None:
        found = self._joined(
            bs.BedrockSeed(REGION, residency_regions=("ap-southeast-2", "ap-southeast-4"))
        )
        assert found["au.anthropic.claude-new-v1:0"].outcome is DiscoveryOutcome.ALLOWED
        old = found["au.anthropic.claude-old-v1:0"]
        assert old.outcome is DiscoveryOutcome.REFUSED
        assert old.reason is not None and "ap-southeast-6" in old.reason

    def test_the_default_residency_refuses_every_au_profile(self) -> None:
        found = self._joined(bs.BedrockSeed(REGION))
        assert all(m.outcome is DiscoveryOutcome.REFUSED for m in found.values())

    def test_residency_regions_become_the_bedrock_processing_choice(self) -> None:
        connection = bc.build(
            bs.BedrockSeed(REGION, residency_regions=("ap-southeast-4", "ap-southeast-2"))
        )
        assert connection.settings["processing"] == {
            "regions": ["ap-southeast-2", "ap-southeast-4"],
            "allow_global": False,
        }

    def test_no_residency_regions_leave_processing_unset(self) -> None:
        assert "processing" not in bc.build(bs.BedrockSeed(REGION)).settings

    def test_a_region_conduit_refuses_is_refused_with_its_reason(self) -> None:
        # Valid in form, but not a Region where the bedrock-runtime endpoint is offered.
        connection = bc.build(bs.BedrockSeed("xx-nowhere-9"))
        registry = ProviderRegistry(
            secrets=LiteralSecretResolver({}), factories=production_factories()
        )
        with pytest.raises(ConnectionInvalid) as refused:
            validate(connection, registry)
        assert "region" in str(refused.value)


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

    async def test_no_models_setting_stores_no_model_rows(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        await bc.seed_bedrock(spark_db, bs.BedrockSeed(REGION), actor="spark-bootstrap")
        assert await DatabaseConnectionStore(spark_db).list_models("aws-bedrock") == []
        assert "models" not in audited[0]["details"]

    async def test_the_models_setting_creates_the_connection_curated(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        ids = tuple(MODELS.split(","))
        seed = bs.BedrockSeed(REGION, models=ids)
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "created"
        rows = await DatabaseConnectionStore(spark_db).list_models("aws-bedrock")
        assert sorted((m.model_id, m.enabled, m.tier) for m in rows) == sorted(
            (i, True, "standard") for i in ids
        )
        assert await _marked(spark_db)
        assert audited[0]["details"]["models"] == list(ids)

    async def test_the_models_setting_never_changes_an_existing_connection(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        await _store_one(spark_db)
        seed = bs.BedrockSeed(REGION, models=("amazon.nova-lite-v1:0",))
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "exists"
        assert await DatabaseConnectionStore(spark_db).list_models("aws-bedrock") == []

    async def test_a_deleted_curated_connection_is_not_curated_again(
        self, spark_db: Any, audited: list[dict[str, Any]]
    ) -> None:
        seed = bs.BedrockSeed(REGION, models=("amazon.nova-lite-v1:0",))
        await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap")
        assert await DatabaseConnectionStore(spark_db).delete_connection("aws-bedrock")
        assert await bc.seed_bedrock(spark_db, seed, actor="spark-bootstrap") == "declined"
        assert await DatabaseConnectionStore(spark_db).list_models("aws-bedrock") == []

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
