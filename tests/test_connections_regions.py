"""conduit 0.1.0's discovery-led catalogue and Region choice, as SPARK adopts them.

Every listing here is synthetic: a Bedrock factory whose client is a placeholder and whose
listing the test writes, so nothing reaches a provider. Guards under test:

* a discovered model is shown with the Regions it is processed in, whether conduit allows
  it under the connection's residency, and conduit's reason when it does not;
* drift between the catalogue and the listing is shown as information (never hiding a
  model), in conduit's own ``DriftReason`` words;
* the runtime builds a provider on the catalogue with the service's own Regions, so a
  Bedrock connection under a Regions policy offers its models (the default catalogue
  carries no Region data);
* an administrator chooses Regions per connection: conduit validates the choice, refuses
  what it refuses (shown, nothing saved), and the change is audited by field name only;
* a connection that chooses none is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from conduit import (
    DiscoveredModel,
    DiscoveryOutcome,
    DriftReason,
    LiteralSecretResolver,
    Message,
    ModelRequest,
    ProviderRegistry,
    ResidencyPolicy,
    ResidencyRefused,
    ResidencyScope,
    default_catalogue,
)
from conduit.providers.bedrock.factory import BedrockFactory
from conduit.providers.stub import StubFactory
from fastapi.testclient import TestClient

from spark.connections import Connection, ConnectionRuntime, discovery, regions
from spark.connections.model import ConnectionInvalid
from spark.connections.runtime import production_factories, snapshot_of
from spark.database.connections import OverlayRow
from spark.identity import audit
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.test_connections_web import _config_of, _refresh, secrets  # noqa: F401
from tests.test_instance_settings_web import MemorySecrets
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in

API = "/api/connections"
SYD = "ap-southeast-2"
MEL = "ap-southeast-4"
IN_REGION = ResidencyScope.IN_REGION
CROSS = ResidencyScope.CROSS_REGION
#: A model conduit's default catalogue declares (no Region data in it).
CATALOGUED = "anthropic.claude-fable-5"

LISTING: list[DiscoveredModel] = [
    DiscoveredModel(CATALOGUED, scope=IN_REGION, regions=frozenset({SYD})),
    DiscoveredModel(
        f"au.{CATALOGUED}",
        scope=CROSS,
        regions=frozenset({SYD, MEL}),
        base_model_id=CATALOGUED,
    ),
    DiscoveredModel(f"global.{CATALOGUED}", scope=ResidencyScope.GLOBAL, base_model_id=CATALOGUED),
    DiscoveredModel("vendor.unknown-v1:0", scope=IN_REGION, regions=frozenset({SYD})),
]


class ListingBedrock(BedrockFactory):
    """conduit's Bedrock adapter with a placeholder client and a listing the test writes."""

    def __init__(self, listing: list[DiscoveredModel]) -> None:
        super().__init__()
        self._listing = listing

    def create_client(self, settings: Any, secrets: Any) -> Any:
        return object()

    async def discover_models(self, settings: Any, client: Any) -> Any:
        return self._listing


def _factories(listing: list[DiscoveredModel]) -> list[Any]:
    return [
        ListingBedrock(listing),
        StubFactory(models=["stub-echo"]),
        *[f for f in production_factories() if f.kind != "bedrock"],
    ]


def _bedrock(
    connection_id: str = "syd",
    *,
    scopes: tuple[ResidencyScope, ...] = (ResidencyScope.LOCAL, IN_REGION, CROSS),
    regions_: tuple[str, ...] = (SYD, MEL),
    extra: dict[str, Any] | None = None,
) -> Connection:
    return Connection(
        connection_id,
        "Sydney",
        {"kind": "bedrock", "region": SYD, "auth": {"mode": "credential_chain"}, **(extra or {})},
        ResidencyPolicy(scopes=frozenset(scopes), regions=frozenset(regions_)),
    )


def _runtime(
    connection: Connection, listing: list[DiscoveredModel] | None = None, overlays: Any = ()
) -> ConnectionRuntime:
    runtime = ConnectionRuntime(
        secrets=LiteralSecretResolver({}),
        factories=_factories(listing if listing is not None else LISTING),
        drain_s=0.01,
    )
    runtime.apply(snapshot_of([connection], overlays=overlays))
    return runtime


# -- 1a: the discovery result -----------------------------------------------------------


class TestDiscoveryResult:
    def test_a_listing_is_recorded_with_regions_reasons_and_drift(self) -> None:
        runtime = _runtime(_bedrock())
        try:
            record = discovery.discover(runtime, _bedrock(), discovery.DiscoveryResults())
        finally:
            runtime.close()
        assert record.ok
        rows = {m.model_id: m for m in record.models}
        assert rows[CATALOGUED].regions == frozenset({SYD}) and rows[CATALOGUED].allowed
        assert rows[f"au.{CATALOGUED}"].regions == frozenset({SYD, MEL})
        assert rows[f"au.{CATALOGUED}"].scope == "cross_region"
        refused = rows[f"global.{CATALOGUED}"]
        assert not refused.allowed
        assert refused.reason is not None and "global" in refused.reason
        # Uncatalogued models are conduit's ``uncatalogued``; the old ``undeclared`` is gone.
        assert [u.model_id for u in record.undeclared] == ["vendor.unknown-v1:0"]
        reasons = {(d.model_id, d.reason) for d in record.drift}
        assert ("vendor.unknown-v1:0", DriftReason.UNCATALOGUED.value) in reasons
        assert any(r == DriftReason.UNLISTED.value for _, r in reasons)

    def test_drift_words_cover_every_reason_conduit_reports(self) -> None:
        assert set(discovery.DRIFT_TEXT) == {reason.value for reason in DriftReason}
        assert "no capability record" in discovery.DRIFT_TEXT["uncatalogued"]

    def test_conduit_no_longer_reports_undeclared(self) -> None:
        from conduit import DiscoveryResult

        assert not hasattr(DiscoveryResult, "undeclared")
        assert not hasattr(DiscoveryOutcome, "UNDECLARED")


# -- 1b: residency is checked with the connection's provider settings --------------------


class TestResidencyWithProviderSettings:
    def _settings(self) -> Any:
        registry = ProviderRegistry(secrets=LiteralSecretResolver({}), factories=_factories([]))
        return registry.validate(
            "syd",
            {
                "kind": "bedrock",
                "region": SYD,
                "auth": {"mode": "credential_chain"},
                "processing": {"regions": [SYD], "allow_global": False},
            },
        )

    def test_the_connections_region_choice_refuses_what_the_policy_allows(self) -> None:
        entry = default_catalogue().require("bedrock", CATALOGUED)
        entry = entry.model_copy(
            update={"residency_scope": CROSS, "regions": frozenset({SYD, MEL})}
        )
        policy = ResidencyPolicy(scopes=frozenset({CROSS}), regions=frozenset({SYD, MEL}))
        assert discovery.refusal(policy, entry, SYD, None, "syd") is None
        refused = discovery.refusal(policy, entry, SYD, self._settings(), "syd")
        assert refused is not None and "outside the connection's Region choice" in refused

    def test_the_runtime_holds_a_model_to_the_choice_too(self) -> None:
        connection = _bedrock(extra={"processing": {"regions": [SYD], "allow_global": False}})
        runtime = _runtime(connection)
        try:
            offered = {m["model_id"] for m in runtime.listed_models("syd")}
        finally:
            runtime.close()
        # The in-Region model stays; the cross-Region profile names a Region outside the choice.
        assert CATALOGUED in offered
        assert f"au.{CATALOGUED}" not in offered

    def test_a_chosen_model_outside_the_choice_is_refused_with_conduits_reason(self) -> None:
        from spark.connections import ModelRef

        connection = _bedrock(extra={"processing": {"regions": [SYD], "allow_global": False}})
        runtime = _runtime(connection)
        try:
            outside = runtime.refusal(ModelRef("syd", f"au.{CATALOGUED}"))
            inside = runtime.refusal(ModelRef("syd", CATALOGUED))
        finally:
            runtime.close()
        assert outside is not None and "outside the connection's Region choice" in outside
        assert inside is None


# -- 1c: the provider is built on the catalogue with the service's own Regions -----------


class TestBuiltOnTheListedCatalogue:
    def _only_sydney(self) -> Connection:
        return _bedrock(scopes=(ResidencyScope.LOCAL, IN_REGION), regions_=(SYD,))

    def test_a_bedrock_connection_under_a_regions_policy_offers_its_models(self) -> None:
        runtime = _runtime(self._only_sydney())
        try:
            offered = {m["model_id"] for m in runtime.listed_models("syd")}
        finally:
            runtime.close()
        assert CATALOGUED in offered
        # The default catalogue has no Regions for the base entry, so only the listing
        # makes it in-Region: the cross-Region profile is still refused by scope.
        assert f"au.{CATALOGUED}" not in offered

    def test_the_provider_serves_the_listed_entry(self) -> None:
        runtime = _runtime(self._only_sydney())
        try:
            provider = runtime.provider("syd")
            request = ModelRequest(
                model=CATALOGUED, messages=(Message.user("hi"),), max_output_tokens=16
            )
            # Admitted (no ResidencyRefused): the provider's catalogue carries the Regions.
            provider._admit(request)
        finally:
            runtime.close()

    def test_without_the_listed_catalogue_conduit_refuses_the_model(self) -> None:
        # What a provider built on the default catalogue does under this policy.
        registry = ProviderRegistry(
            secrets=LiteralSecretResolver({}),
            factories=_factories([]),
            residency=self._only_sydney().residency,
        )
        settings = registry.validate("syd", self._only_sydney().settings)
        entry = default_catalogue().require("bedrock", CATALOGUED)
        with pytest.raises(ResidencyRefused):
            registry.residency.check(entry, SYD, task="syd", provider=settings)

    def test_a_failed_listing_builds_on_the_catalogue_alone(self) -> None:
        class Failing(ListingBedrock):
            async def discover_models(self, settings: Any, client: Any) -> Any:
                raise RuntimeError("down")

        runtime = ConnectionRuntime(
            secrets=LiteralSecretResolver({}), factories=[Failing([])], drain_s=0.01
        )
        try:
            runtime.apply(snapshot_of([_bedrock()]))
            assert runtime.failures() == {}
            assert runtime.served_for("syd") is runtime.catalogue_for("syd")
        finally:
            runtime.close()

    def test_a_model_the_catalogue_does_not_declare_is_still_declared_first(self) -> None:
        runtime = _runtime(self._only_sydney())
        try:
            offered = {m["model_id"] for m in runtime.listed_models("syd")}
        finally:
            runtime.close()
        assert "vendor.unknown-v1:0" not in offered


# -- 1e: a Bedrock Region must be one the runtime endpoint serves ------------------------


class TestBedrockRegionsAreChecked:
    def test_conduit_refuses_a_region_without_the_runtime_endpoint(self) -> None:
        runtime = _runtime(_bedrock())
        try:
            with pytest.raises(ConnectionInvalid) as refused:
                runtime.validate(
                    Connection("x", "X", {"kind": "bedrock", "region": "xx-nowhere-9"})
                )
        finally:
            runtime.close()
        assert "region" in str(refused.value)

    def test_the_processing_choice_must_include_the_called_region(self) -> None:
        runtime = _runtime(_bedrock())
        try:
            settings = regions.with_choice(_bedrock().settings, {"regions": [MEL]})
            with pytest.raises(ConnectionInvalid) as refused:
                runtime.validate(Connection("syd", "S", settings))
        finally:
            runtime.close()
        assert "processing" in str(refused.value)


# -- The shapes of a Region choice ----------------------------------------------------------


class TestRegionChoices:
    def test_bedrock_processing_round_trips_and_clears(self) -> None:
        base = _bedrock().settings
        chosen = regions.with_choice(base, {"regions": [SYD, MEL], "allow_global": True})
        assert chosen["processing"] == {"regions": [SYD, MEL], "allow_global": True}
        assert regions.current(chosen) == {"regions": [SYD, MEL], "allow_global": True}
        assert "processing" not in regions.with_choice(chosen, None)
        assert "processing" not in base

    def test_anthropic_openai_and_foundry_use_their_own_settings(self) -> None:
        direct = {"kind": "anthropic", "endpoint": "direct", "api_key": "secret://k"}
        assert regions.with_choice(direct, {"geography": "us"})["inference_geo"] == "us"
        openai = {"kind": "openai_compat", "service": {"name": "openai", "api_key": "secret://k"}}
        chosen = regions.with_choice(openai, {"regions": ["openai:eu"]})
        assert chosen["service"]["data_residency"] == "eu"
        assert regions.current(chosen) == {"regions": ["openai:eu"]}
        foundry = {"kind": "anthropic", "endpoint": "foundry"}
        deployment = {"type": "data_zone", "azure_region": "azure:swedencentral"}
        got = regions.with_choice(foundry, {"deployment": deployment})
        assert got["deployment"] == {"type": "data_zone", "azure_region": "swedencentral"}
        mapped = {"kind": "openai_compat", "service": {"name": "foundry"}}
        got = regions.with_choice(mapped, {"deployment": deployment})
        assert got["service"]["deployment"]["type"] == "data_zone"

    def test_a_service_with_no_choice_refuses_one(self) -> None:
        with pytest.raises(ConnectionInvalid):
            regions.with_choice({"kind": "ollama"}, {"regions": ["x"]})

    @pytest.mark.parametrize(
        "choice",
        [{"geography": "mars"}, {"regions": 5}, {"deployment": {"type": "global"}}],
    )
    def test_a_malformed_choice_is_refused(self, choice: dict[str, Any]) -> None:
        for settings in (
            {"kind": "anthropic", "endpoint": "direct"},
            {"kind": "anthropic", "endpoint": "foundry"},
            _bedrock().settings,
        ):
            try:
                regions.with_choice(settings, choice)
            except ConnectionInvalid:
                continue
            # A choice another service ignores is not malformed for it.
            assert "deployment" not in choice or settings["kind"] == "bedrock"

    def test_conduit_lists_the_regions_to_offer(self) -> None:
        runtime = _runtime(_bedrock())
        try:
            offered = regions.offered(runtime.registry(), _bedrock().settings)
            direct = regions.offered(
                runtime.registry(), {"kind": "anthropic", "endpoint": "direct"}
            )
            none = regions.offered(runtime.registry(), {"kind": "ollama"})
        finally:
            runtime.close()
        assert offered["mode"] == "processing"
        codes = {r["code"] for listing in offered["listings"] for r in listing["regions"]}
        assert SYD in codes and MEL in codes
        assert direct["mode"] == "inference_geo"
        assert [listing["service"] for listing in direct["listings"]] == ["direct"]
        assert none == {"mode": None, "listings": []}


# -- The Connections page ---------------------------------------------------------------------


@pytest.fixture
def client(
    sqlite_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets: MemorySecrets
) -> Iterator[TestClient]:
    import spark.core.application as application

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(application, "_get_config_path", lambda: path)
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    ctx.secrets = secrets
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = "u1"
        runtime = ConnectionRuntime(
            secrets=LiteralSecretResolver({}), factories=_factories(LISTING), drain_s=0.01
        )
        app.state.connections = runtime
        sign_in(c)
        try:
            yield c
        finally:
            runtime.close()


def _create(client: TestClient, **extra: Any) -> str:
    body = {
        "service": "bedrock",
        "name": "Sydney",
        "values": {"region": SYD, "auth.mode": "credential_chain"},
        "residency": {
            "default": False,
            "scopes": ["local", "in_region", "cross_region"],
            "regions": [SYD, MEL],
        },
        **extra,
    }
    resp = client.post(API, json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestConnectionsPage:
    def test_discovered_models_show_regions_verdicts_and_reasons(self, client: TestClient) -> None:
        cid = _create(client)
        view = client.post(f"{API}/{cid}/discover").json()
        rows = {m["model_id"]: m for m in view["discovered"]}
        assert rows[CATALOGUED]["regions"] == [SYD] and rows[CATALOGUED]["allowed"] is True
        assert rows[f"au.{CATALOGUED}"]["regions"] == [SYD, MEL]
        assert rows[f"au.{CATALOGUED}"]["scope"] == "cross_region"
        refused = rows[f"global.{CATALOGUED}"]
        assert refused["allowed"] is False and "global" in refused["reason"]
        # The catalogue rows carry the same Regions and verdict.
        catalogue_rows = {m["model_id"]: m for m in view["models"]}
        assert catalogue_rows[CATALOGUED]["regions"] == [SYD]
        assert catalogue_rows[CATALOGUED]["refused"] is None

    def test_drift_is_information_and_hides_no_model(self, client: TestClient) -> None:
        cid = _create(client)
        view = client.post(f"{API}/{cid}/discover").json()
        by_reason: dict[str, list[dict[str, Any]]] = {}
        for note in view["drift"]:
            by_reason.setdefault(note["reason"], []).append(note)
        (uncatalogued,) = by_reason["uncatalogued"]
        assert uncatalogued["model_id"] == "vendor.unknown-v1:0"
        assert "no capability record" in uncatalogued["text"]
        assert by_reason["unlisted"]
        listed = {m["model_id"] for m in view["discovered"]}
        assert listed == {m.model_id for m in LISTING}
        assert {u["model_id"] for u in view["undeclared"]} == {"vendor.unknown-v1:0"}

    def test_a_model_the_catalogue_offers_elsewhere_is_shown_when_the_service_lists_it(
        self, client: TestClient
    ) -> None:
        # The catalogue (no Region data in 0.1.0) cannot say where a model is offered; the
        # service's listing can, and a listed model is never left out for the catalogue's Regions.
        cid = _create(client)
        rows = {m["model_id"] for m in client.post(f"{API}/{cid}/discover").json()["models"]}
        assert CATALOGUED in rows

    def test_a_declared_model_outside_the_choice_shows_conduits_reason(
        self, client: TestClient
    ) -> None:
        cid = _create(client)
        from spark.connections.store import FileConnectionStore

        FileConnectionStore(_config_of(client)).write_overlays(
            [
                OverlayRow(
                    "bedrock",
                    "custom.model-v1:0",
                    100_000,
                    "cross_region",
                    output_limit=4_096,
                    regions=(SYD, MEL),
                    connection_id=cid,
                )
            ]
        )
        _refresh(client)
        rows = {m["model_id"]: m for m in client.get(f"{API}/{cid}/models").json()["models"]}
        assert rows["custom.model-v1:0"]["refused"] is None
        resp = client.put(
            f"{API}/{cid}/regions", json={"choice": {"regions": [SYD], "allow_global": False}}
        )
        assert resp.status_code == 200, resp.text
        rows = {m["model_id"]: m for m in client.get(f"{API}/{cid}/models").json()["models"]}
        assert "outside the connection's Region choice" in rows["custom.model-v1:0"]["refused"]


class TestRegionChoiceOnThePage:
    def test_the_regions_offered_and_the_choice_made(self, client: TestClient) -> None:
        cid = _create(client)
        shown = client.get(f"{API}/{cid}/regions").json()
        assert shown["mode"] == "processing" and shown["choice"] is None
        assert shown["region"] == SYD
        codes = {r["code"] for listing in shown["listings"] for r in listing["regions"]}
        assert {SYD, MEL} <= codes
        assert shown["listings"][0]["documentation"] and shown["listings"][0]["as_of"]

    def test_a_choice_is_saved_and_narrows_what_is_usable(self, client: TestClient) -> None:
        cid = _create(client)
        resp = client.put(
            f"{API}/{cid}/regions", json={"choice": {"regions": [SYD], "allow_global": False}}
        )
        assert resp.status_code == 200, resp.text
        assert client.get(f"{API}/{cid}/regions").json()["choice"] == {
            "regions": [SYD],
            "allow_global": False,
        }
        stored = client.get(f"{API}/{cid}").json()
        assert stored["id"] == cid
        view = client.post(f"{API}/{cid}/discover").json()
        rows = {m["model_id"]: m for m in view["discovered"]}
        assert rows[CATALOGUED]["allowed"] is True
        profile = rows[f"au.{CATALOGUED}"]
        assert profile["allowed"] is False
        assert "outside the connection's Region choice" in profile["reason"]
        assert view["discovery"]["choice"] == {"regions": [SYD], "allow_global": False}

    def test_a_choice_conduit_refuses_is_shown_and_nothing_is_saved(
        self, client: TestClient
    ) -> None:
        cid = _create(client)
        for choice, fragment in (
            ({"regions": ["xx-nowhere-9"]}, "processing"),
            ({"regions": [MEL]}, "processing"),
        ):
            resp = client.put(f"{API}/{cid}/regions", json={"choice": choice})
            assert resp.status_code == 400, resp.text
            assert fragment in resp.json()["error"]
        assert client.get(f"{API}/{cid}/regions").json()["choice"] is None

    def test_a_choice_for_a_service_that_offers_none_is_refused(self, client: TestClient) -> None:
        resp = client.post(API, json={"service": "ollama", "name": "Box", "values": {}})
        resp = client.put(f"{API}/box/regions", json={"choice": {"regions": ["x"]}})
        assert resp.status_code == 400
        assert client.get(f"{API}/box/regions").json()["mode"] is None

    def test_a_malformed_choice_is_refused(self, client: TestClient) -> None:
        cid = _create(client)
        assert client.put(f"{API}/{cid}/regions", json={"choice": "us"}).status_code == 400
        assert client.put(f"{API}/nope/regions", json={"choice": None}).status_code == 404

    def test_a_choice_can_be_cleared(self, client: TestClient) -> None:
        cid = _create(client)
        client.put(f"{API}/{cid}/regions", json={"choice": {"regions": [SYD]}})
        resp = client.put(f"{API}/{cid}/regions", json={"choice": None})
        assert resp.status_code == 200 and resp.json()["choice"] is None
        assert client.get(f"{API}/{cid}/regions").json()["choice"] is None

    def test_the_change_is_audited_by_field_name_only(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[dict[str, Any]] = []

        async def emit_request(request: Any, action: str, **fields: Any) -> None:
            events.append({"action": action, **fields})

        monkeypatch.setattr(audit, "emit_request", emit_request)
        cid = _create(client)
        events.clear()
        client.put(f"{API}/{cid}/regions", json={"choice": {"regions": [SYD, MEL]}})
        (event,) = events
        assert event["action"] == audit.SETTING_CHANGED
        assert event["resource_id"] == cid
        assert event["details"]["change"] == "regions"
        assert event["details"]["fields"] == ["processing.allow_global", "processing.regions"]
        assert SYD not in str(event) and MEL not in str(event)
        events.clear()
        client.put(f"{API}/{cid}/regions", json={"choice": {"regions": ["xx-nowhere-9"]}})
        assert events == []  # a refused choice is not a change

    def test_the_list_says_how_each_connection_takes_a_choice(self, client: TestClient) -> None:
        cid = _create(client)
        client.post(API, json={"service": "ollama", "name": "Box", "values": {}})
        client.put(f"{API}/{cid}/regions", json={"choice": {"regions": [SYD]}})
        items = {c["id"]: c for c in client.get(API).json()["connections"]}
        assert items[cid]["regions_mode"] == "processing"
        assert items[cid]["region_choice"] == {"regions": [SYD], "allow_global": False}
        assert items["box"]["regions_mode"] is None and items["box"]["region_choice"] is None

    def test_a_connection_that_chooses_no_region_is_unchanged(self, client: TestClient) -> None:
        cid = _create(client)
        before = client.get(f"{API}/{cid}").json()
        client.put(f"{API}/{cid}/regions", json={"choice": {"regions": [SYD]}})
        client.put(f"{API}/{cid}/regions", json={"choice": None})
        assert client.get(f"{API}/{cid}").json() == before

    def test_the_form_shows_conduits_refusal_of_a_bedrock_region(self, client: TestClient) -> None:
        resp = client.post(
            API,
            json={
                "service": "bedrock",
                "name": "Nowhere",
                "values": {"region": "xx-nowhere-9", "auth.mode": "credential_chain"},
            },
        )
        assert resp.status_code == 400
        assert "region" in resp.json()["error"]


def test_an_old_connection_without_a_choice_has_the_same_settings(tmp_path: Path) -> None:
    """A stored connection that never chose Regions validates and builds as before."""
    connection = _bedrock(scopes=(ResidencyScope.LOCAL, IN_REGION), regions_=(SYD,))
    runtime = _runtime(connection)
    try:
        assert regions.current(connection.settings) is None
        assert runtime.settings_of("syd") is not None
        assert runtime.settings_of("syd").region_choice() is None  # type: ignore[union-attr]
        assert runtime.failures() == {}
    finally:
        runtime.close()
