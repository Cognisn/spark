"""Model discovery through conduit and the models view (M5), on conduit's stub provider.

No network: the stub's factory lists what the test gives it. Guards under test:
a listed model is reachable, one the catalogue lacks is undeclared and shown apart,
a failure is recorded with conduit's failure class only, a model the residency
policy refuses carries conduit's reason, and a model the catalogue offers only in
other Regions is left out.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conduit import (
    DiscoveredModel,
    LiteralSecretResolver,
    ProviderError,
    ProviderFailure,
    ResidencyPolicy,
    ResidencyScope,
)
from conduit.providers.stub import StubFactory

from spark.connections import Connection, ConnectionModel, ConnectionRuntime, discovery
from spark.connections.runtime import snapshot_of
from spark.database.connections import OverlayRow
from tests.model_helpers import stub_connection

LOCAL = ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL}))


class FailingStub(StubFactory):
    async def discover_models(self, settings: Any, client: Any) -> Any:
        raise ProviderError(ProviderFailure.REJECTED)


def _runtime(factory: StubFactory, overlays: list[OverlayRow] | None = None) -> ConnectionRuntime:
    runtime = ConnectionRuntime(
        secrets=LiteralSecretResolver({}), factories=[factory], drain_s=0.01
    )
    runtime.apply(snapshot_of([stub_connection("lab")], overlays=overlays or []))
    return runtime


@pytest.fixture
def results() -> Iterator[discovery.DiscoveryResults]:
    yield discovery.DiscoveryResults()


def test_listed_models_are_reachable_and_unknown_ones_undeclared(
    results: discovery.DiscoveryResults,
) -> None:
    factory = StubFactory(models=["stub-echo", DiscoveredModel("pulled:7b", label="local")])
    runtime = _runtime(factory)
    try:
        record = discovery.discover(runtime, stub_connection("lab"), results)
    finally:
        runtime.close()
    assert record.ok
    assert record.reachable == frozenset({"stub-echo", "pulled:7b"})
    assert record.undeclared == (discovery.Undeclared("pulled:7b", "local"),)
    assert results.get("lab") is record
    assert record.summary() == "1 model reachable, 1 not in the catalogue"


def test_a_model_declared_on_another_connection_is_undeclared_here(
    results: discovery.DiscoveryResults,
) -> None:
    factory = StubFactory(models=["stub-echo", DiscoveredModel("pulled:7b", label="local")])
    declared = OverlayRow(
        "stub", "pulled:7b", 8_192, "local", output_limit=1_024, connection_id="other"
    )
    runtime = ConnectionRuntime(
        secrets=LiteralSecretResolver({}), factories=[factory], drain_s=0.01
    )
    try:
        runtime.apply(
            snapshot_of(
                [stub_connection("lab"), stub_connection("other")],
                overlays=[declared],
            )
        )
        here = discovery.discover(runtime, stub_connection("lab"), results)
        there = discovery.discover(runtime, stub_connection("other"), results)
    finally:
        runtime.close()
    assert here.undeclared == (discovery.Undeclared("pulled:7b", "local"),)
    assert there.undeclared == ()
    assert "pulled:7b" in there.reachable


def test_a_failed_listing_is_recorded_with_the_failure_class_only(
    results: discovery.DiscoveryResults,
) -> None:
    runtime = _runtime(FailingStub(models=["stub-echo"]))
    try:
        record = discovery.discover(runtime, stub_connection("lab"), results)
    finally:
        runtime.close()
    assert not record.ok
    assert record.error == "the service refused the request (check the credentials)"
    assert record.summary().startswith("Failed: ")


def test_invalid_settings_are_recorded_with_conduits_refusal(
    results: discovery.DiscoveryResults,
) -> None:
    runtime = _runtime(StubFactory())
    broken = Connection("lab", "Lab", {"kind": "stub"}, LOCAL)
    try:
        record = discovery.discover(runtime, broken, results)
    finally:
        runtime.close()
    assert record.error is not None and "settings refused" in record.error


def _overlay(model: str, scope: str = "local", regions: tuple[str, ...] = ()) -> OverlayRow:
    return OverlayRow(
        "stub", model, 100_000, scope, output_limit=4_096, regions=regions, connection_id="lab"
    )


class TestModelsView:
    def _view(
        self,
        connection: Connection,
        stored: list[ConnectionModel] | None,
        record: discovery.DiscoveryRecord | None,
    ) -> dict[str, Any]:
        runtime = _runtime(
            StubFactory(),
            [
                _overlay("model-a"),
                _overlay("model-g", "global"),
                _overlay("model-syd", "in_region", ("ap-southeast-2",)),
                _overlay("model-iad", "in_region", ("us-east-1",)),
            ],
        )
        try:
            return discovery.models_view(
                connection,
                runtime.catalogue_for("lab"),
                "stub",
                stored,
                record,
                {("stub", "model-a")},
            )
        finally:
            runtime.close()

    def test_rows_join_catalogue_discovery_residency_and_stored_rows(self) -> None:
        connection = Connection(
            "lab",
            "Lab",
            {"kind": "stub", "not_for_production": True, "region": "ap-southeast-2"},
            ResidencyPolicy(scopes=frozenset({ResidencyScope.LOCAL, ResidencyScope.IN_REGION})),
        )
        record = discovery.DiscoveryRecord(
            "lab",
            "2026-11-18T00:00:00+00:00",
            reachable=frozenset({"model-a"}),
            models=(discovery.DiscoveredRow("model-a"), discovery.DiscoveredRow("new:1b")),
        )
        view = self._view(connection, [ConnectionModel("lab", "model-a", True, "higher")], record)
        rows = {m["model_id"]: m for m in view["models"]}
        # Offered in another Region only: left out; embeddings: left out.
        assert "model-iad" not in rows and "stub-embed" not in rows
        assert rows["model-a"]["reachable"] is True and rows["model-a"]["overlay"] is True
        assert rows["model-a"]["tier"] == "higher" and rows["model-a"]["enabled"] is True
        assert rows["model-syd"]["reachable"] is False and rows["model-syd"]["enabled"] is False
        assert rows["model-syd"]["refused"] is None
        assert "scope global is not allowed" in rows["model-g"]["refused"]
        assert view["undeclared"] == [{"model_id": "new:1b", "label": None}]
        assert view["curated"] is True

    def test_an_uncurated_connection_has_every_model_enabled_and_no_reachability(self) -> None:
        view = self._view(stub_connection("lab"), None, None)
        assert view["curated"] is False
        assert all(m["enabled"] and m["reachable"] is None for m in view["models"])
        assert view["discovery"] is None
