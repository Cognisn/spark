"""Choosing a connection's models: discovered models are chosen like catalogue models.

A Bedrock connection lists inference profiles (``au.anthropic...``) whose foundation model
the catalogue declares, so conduit serves each under the profile's own id. The Models dialog
shows them in the one list with an Enabled switch, the first choice keeps every model the
connection offered, and the picker offers exactly the enabled models conduit allows.

Here conduit's stub stands in: ``profile.stub-echo`` is a listed profile of the catalogued
``stub-echo`` (local), ``far.stub-echo`` one the residency policy refuses (global), and
``pulled:1b`` a listed model the catalogue does not declare at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from conduit import DiscoveredModel, LiteralSecretResolver, ResidencyScope
from conduit.providers.stub import StubFactory
from fastapi.testclient import TestClient

from spark.connections import Connection, ConnectionModel, ConnectionRuntime, discovery
from spark.connections.runtime import snapshot_of
from spark.connections.store import FileConnectionStore
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.model_helpers import stub_connection
from tests.test_instance_settings_web import MemorySecrets
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in

API = "/api/connections"
LOCAL = ResidencyScope.LOCAL
PROFILE = "profile.stub-echo"
SECOND = "second.stub-echo"
FAR = "far.stub-echo"


def listing(*extra: str) -> list[str | DiscoveredModel]:
    """What the stub lists: the catalogued models, profiles of them, and an undeclared one."""
    found: list[str | DiscoveredModel] = [
        "stub-echo",
        DiscoveredModel(PROFILE, scope=LOCAL, base_model_id="stub-echo"),
        DiscoveredModel(SECOND, scope=LOCAL, base_model_id="stub-echo"),
        DiscoveredModel(FAR, scope=ResidencyScope.GLOBAL, base_model_id="stub-echo"),
        DiscoveredModel("pulled:1b", label="local", scope=LOCAL),
    ]
    found.extend(DiscoveredModel(m, scope=LOCAL, base_model_id="stub-echo") for m in extra)
    return found


class ListingStub(StubFactory):
    """A stub whose listing a test changes between provider builds."""

    def __init__(self, models: Sequence[str | DiscoveredModel]) -> None:
        super().__init__(models=models)
        self.models = list(models)

    async def discover_models(self, settings: Any, client: Any) -> Any:
        return tuple(
            m if isinstance(m, DiscoveredModel) else DiscoveredModel(m, scope=LOCAL)
            for m in self.models
        )


@pytest.fixture(autouse=True)
def _fresh_results() -> Iterator[None]:
    discovery.RESULTS.clear()
    yield
    discovery.RESULTS.clear()


def runtime_on(factory: ListingStub, stored: Sequence[ConnectionModel] | None = None) -> Any:
    runtime = ConnectionRuntime(
        secrets=LiteralSecretResolver({}), factories=[factory], drain_s=0.01
    )
    runtime.apply(snapshot_of([stub_connection("lab")], models=stored or ()))
    return runtime


def offered(runtime: ConnectionRuntime) -> set[str]:
    return {m["model_id"] for m in runtime.listed_models("lab")}


class TestOffering:
    def test_a_connection_with_no_choices_offers_every_allowed_chat_model(self) -> None:
        runtime = runtime_on(ListingStub(listing()))
        try:
            assert offered(runtime) == {"stub-echo", PROFILE, SECOND}
        finally:
            runtime.close()

    def test_a_disabled_discovered_model_is_not_offered(self) -> None:
        stored = [
            ConnectionModel("lab", "stub-echo"),
            ConnectionModel("lab", PROFILE, enabled=False),
            ConnectionModel("lab", SECOND),
        ]
        runtime = runtime_on(ListingStub(listing()), stored)
        try:
            assert offered(runtime) == {"stub-echo", SECOND}
        finally:
            runtime.close()

    def test_a_model_first_discovered_after_curation_is_off(self) -> None:
        stored = [ConnectionModel("lab", "stub-echo"), ConnectionModel("lab", PROFILE)]
        runtime = runtime_on(ListingStub(listing("brand.new")), stored)
        try:
            assert offered(runtime) == {"stub-echo", PROFILE}
        finally:
            runtime.close()

    def test_a_model_that_leaves_the_listing_keeps_its_choice_and_returns_with_it(self) -> None:
        stored = [
            ConnectionModel("lab", "stub-echo"),
            ConnectionModel("lab", PROFILE, tier="higher"),
            ConnectionModel("lab", SECOND, enabled=False),
        ]
        factory = ListingStub([m for m in listing() if getattr(m, "model_id", m) != PROFILE])
        runtime = runtime_on(factory, stored)
        try:
            assert offered(runtime) == {"stub-echo"}
            # The choice lives in the stored rows, so it is there when the model is back.
            factory.models = listing()
            runtime.apply(snapshot_of([_rebuilt("lab")], models=stored))
            tiers = {m["model_id"]: m["tier"] for m in runtime.listed_models("lab")}
            assert tiers == {"stub-echo": "standard", PROFILE: "higher"}
        finally:
            runtime.close()

    def test_a_disabled_model_a_conversation_is_pinned_to_still_resolves(self) -> None:
        stored = [
            ConnectionModel("lab", "stub-echo"),
            ConnectionModel("lab", PROFILE, enabled=False),
        ]
        runtime = runtime_on(ListingStub(listing()), stored)
        try:
            assert not runtime.is_offered(PROFILE)
            ref = runtime.resolve(PROFILE)
            assert ref is not None and ref.model_id == PROFILE
            assert runtime.resolve("lab/" + PROFILE) is not None
        finally:
            runtime.close()


def _rebuilt(connection_id: str) -> Connection:
    """The stub connection with a changed setting, so its provider is rebuilt and lists again."""
    base = stub_connection(connection_id)
    return Connection(
        base.id, base.name, {**base.settings, "chunk_size": 8}, base.residency, base.enabled
    )


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import spark.core.application as application

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(application, "_get_config_path", lambda: path)
    return path


@pytest.fixture
def client(sqlite_settings: Any, config_path: Path) -> Iterator[TestClient]:
    secrets = MemorySecrets()
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    ctx.secrets = secrets
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = "u1"
        runtime = ConnectionRuntime(
            secrets=LiteralSecretResolver({}),
            factories=[ListingStub(listing())],
            drain_s=0.01,
        )
        app.state.connections = runtime
        sign_in(c)
        FileConnectionStore(config_path).write_connections([stub_connection("lab")])
        _refresh(c)
        try:
            yield c
        finally:
            runtime.close()


def _refresh(client: TestClient) -> None:
    from spark.connections.startup import loader
    from spark.core.application import _get_config_path

    app: Any = client.app
    app.state.connections.refresh(
        loader(app.state.sync_db, server=False, config_path=_get_config_path())
    )


def picker(client: TestClient) -> dict[str, str]:
    runtime: Any = client.app.state.connections  # type: ignore[attr-defined]
    return {m["model_id"]: m["tier"] for m in runtime.listed_models("lab")}


def rows_of(response: Any) -> dict[str, Any]:
    return {m["model_id"]: m for m in response.json()["models"]}


class TestModelsDialog:
    def test_one_list_holds_catalogue_and_discovered_models(self, client: TestClient) -> None:
        client.post(f"{API}/lab/discover")
        rows = rows_of(client.get(f"{API}/lab/models"))
        assert {"stub-echo", PROFILE, SECOND, FAR} <= set(rows)
        assert "stub-embed" not in rows, "embedding models are not offered"
        assert rows[PROFILE]["enabled"] and rows[PROFILE]["refused"] is None
        assert rows[PROFILE]["residency_scope"] == "local"
        assert "scope global is not allowed" in rows[FAR]["refused"]
        undeclared = client.get(f"{API}/lab/models").json()["undeclared"]
        assert [u["model_id"] for u in undeclared] == ["pulled:1b"]

    def test_the_first_choice_keeps_every_offered_model(self, client: TestClient) -> None:
        resp = client.put(f"{API}/lab/models", json={"model_id": SECOND, "enabled": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["curated"] is True
        assert picker(client) == {"stub-echo": "standard", PROFILE: "standard"}
        rows = rows_of(resp)
        assert rows[PROFILE]["enabled"] and rows["stub-echo"]["enabled"]
        assert not rows[SECOND]["enabled"]

    def test_a_discovered_model_can_be_enabled_and_set_to_a_tier(self, client: TestClient) -> None:
        client.put(f"{API}/lab/models", json={"model_id": PROFILE, "enabled": False})
        assert PROFILE not in picker(client)
        resp = client.put(
            f"{API}/lab/models", json={"model_id": PROFILE, "enabled": True, "tier": "higher"}
        )
        assert resp.status_code == 200, resp.text
        assert picker(client)[PROFILE] == "higher"

    def test_a_refused_model_cannot_be_enabled(self, client: TestClient) -> None:
        assert (
            client.put(f"{API}/lab/models", json={"model_id": FAR, "enabled": False}).status_code
            == 200
        )
        resp = client.put(f"{API}/lab/models", json={"model_id": FAR, "enabled": True})
        assert resp.status_code == 400
        assert "scope global is not allowed" in resp.json()["error"]
        assert FAR not in picker(client)

    def test_an_unlisted_model_is_still_refused(self, client: TestClient) -> None:
        assert client.put(f"{API}/lab/models", json={"model_id": "nope"}).status_code == 404

    def test_a_choice_survives_a_fresh_discovery_and_a_restart(self, client: TestClient) -> None:
        client.put(f"{API}/lab/models", json={"model_id": PROFILE, "enabled": False})
        client.post(f"{API}/lab/discover")
        assert PROFILE not in picker(client)
        # The choices are stored (config.yaml here, the database in server mode), so a new
        # process reading them sees the same picker.
        _refresh(client)
        assert picker(client) == {"stub-echo": "standard", SECOND: "standard"}
        rows = rows_of(client.get(f"{API}/lab/models"))
        assert not rows[PROFILE]["enabled"] and rows[SECOND]["enabled"]


class TestToday:
    def test_choosing_a_catalogue_model_first_does_not_drop_discovered_ones(
        self, client: TestClient
    ) -> None:
        resp = client.put(f"{API}/lab/models", json={"model_id": "stub-echo", "tier": "higher"})
        assert resp.status_code == 200, resp.text
        assert picker(client) == {"stub-echo": "higher", PROFILE: "standard", SECOND: "standard"}


class TestStoredChoices:
    def test_a_stored_choice_for_a_model_not_served_is_shown_with_its_reason(
        self, client: TestClient
    ) -> None:
        store = FileConnectionStore(_config())
        _store_rows(
            store, ConnectionModel("lab", "stub-echo"), ConnectionModel("lab", "gone.model")
        )
        _refresh(client)
        client.post(f"{API}/lab/discover")
        rows = rows_of(client.get(f"{API}/lab/models"))
        assert rows["gone.model"]["enabled"] is True
        assert rows["gone.model"]["absent"] is True
        assert "does not list this model" in rows["gone.model"]["refused"]
        assert picker(client) == {"stub-echo": "standard"}

    def test_a_stored_absent_model_cannot_be_enabled_from_the_list(
        self, client: TestClient
    ) -> None:
        store = FileConnectionStore(_config())
        _store_rows(
            store, ConnectionModel("lab", "stub-echo"), ConnectionModel("lab", "gone.model", False)
        )
        _refresh(client)
        resp = client.put(f"{API}/lab/models", json={"model_id": "gone.model", "enabled": True})
        assert resp.status_code == 400


def _store_rows(store: FileConnectionStore, *rows: ConnectionModel) -> None:
    async def save() -> None:
        for row in rows:
            await store.save_model(row)

    asyncio.run(save())


def _config() -> Path:
    from spark.core.application import _get_config_path

    return _get_config_path()


def test_the_models_dialog_says_a_model_found_later_is_off_until_enabled() -> None:
    script = (
        Path(__file__).parent.parent / "src" / "spark" / "web" / "static" / "js" / "connections.js"
    ).read_text(encoding="utf-8")
    assert "is off until you switch it on" in script
    assert "cannot be switched on" in script
