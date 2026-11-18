"""The connections screens on the web: personal mode (SQLite, ``config.yaml``).

The real app with personal mode's local user signed in, over a real connection
runtime whose adapters are conduit's own (validation through its registry) plus
its stub (deterministic discovery, no network). Connections are kept in the
test's own ``config.yaml`` and secrets in an in-memory konfig stand-in.

Guards under test:

* secrets are write-only: typed into the modal they go to the secrets store as
  ``connections.<id>.<path>`` and ``config.yaml`` holds only the reference; no
  answer ever carries the value, only set or not set;
* conduit's registry validates before anything is written, and its refusal is shown;
* long-term Bedrock API keys only behind their allow setting;
* delete is refused while a scheduled action uses the connection, naming it;
* the models view marks reachability from discovery, shows conduit's residency
  reason, and the first choice curates the connection without dropping models;
* metering and assignment are server mode only;
* every state-changing route needs the CSRF token and the instance's own Origin.

Server mode's guards are in ``test_connections_web_server.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from conduit import DiscoveredModel
from conduit.integrations.konfig import KonfigSecretResolver
from conduit.providers.stub import StubFactory
from fastapi.testclient import TestClient

from spark.connections import ConnectionRuntime, discovery
from spark.connections.runtime import production_factories
from spark.connections.store import FileConnectionStore
from spark.database import autonomous_actions
from spark.database.connections import OverlayRow, save_overlay
from spark.web.security import CSRF_HEADER
from spark.web.server import create_app
from tests.db_helpers import app_client
from tests.model_helpers import stub_connection
from tests.test_instance_settings_web import MemorySecrets
from tests.test_voice_endpoints import _mock_settings_get
from tests.web_helpers import sign_in

LOCAL = "u1"
API = "/api/connections"
KEY = "sk-ant-example-not-a-real-key"
DISCOVERED = ["stub-echo", DiscoveredModel("pulled:1b", label="local")]


def connection_runtime(secrets: Any) -> ConnectionRuntime:
    """conduit's production adapters (for validation) and its stub (for discovery)."""
    return ConnectionRuntime(
        secrets=KonfigSecretResolver(secrets),
        factories=[StubFactory(models=DISCOVERED), *production_factories()],
        drain_s=0.01,
    )


@pytest.fixture(autouse=True)
def _fresh_results() -> Iterator[None]:
    discovery.RESULTS.clear()
    yield
    discovery.RESULTS.clear()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import spark.core.application as application

    path = tmp_path / "config.yaml"
    monkeypatch.setattr(application, "_get_config_path", lambda: path)
    return path


@pytest.fixture
def secrets() -> MemorySecrets:
    return MemorySecrets()


@pytest.fixture
def client(sqlite_settings: Any, config_path: Path, secrets: MemorySecrets) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    ctx.secrets = secrets
    app = create_app(ctx, first_run=False)
    with app_client(app, sqlite_settings) as c:
        app.state.user_guid = LOCAL
        runtime = connection_runtime(secrets)
        app.state.connections = runtime
        sign_in(c)
        try:
            yield c
        finally:
            runtime.close()


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    return client.app.state.sync_db.call(fn, *args, **kwargs)  # type: ignore[attr-defined]


def _refresh(client: TestClient) -> None:
    from spark.connections.startup import loader

    app: Any = client.app
    app.state.connections.refresh(
        loader(app.state.sync_db, server=False, config_path=_config_of(client))
    )


def _config_of(client: TestClient) -> Path:
    from spark.core.application import _get_config_path

    return _get_config_path()


def _stub(client: TestClient, connection_id: str = "lab") -> None:
    """A stub connection in config.yaml (the modal offers only M4's services)."""
    FileConnectionStore(_config_of(client)).write_connections([stub_connection(connection_id)])
    _refresh(client)


def _anthropic(client: TestClient, name: str = "Claude direct", **extra: Any) -> Any:
    body = {"service": "anthropic", "name": name, "values": {}, "secrets": {"api_key": KEY}}
    body.update(extra)
    return client.post(API, json=body)


def _entries(path: Path) -> list[dict[str, Any]]:
    return list((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("connections", []))


class TestServices:
    def test_the_nine_services_of_m4_with_their_fields(self, client: TestClient) -> None:
        data = client.get(f"{API}/services").json()
        assert [s["key"] for s in data["services"]] == [
            "bedrock",
            "bedrock_mantle",
            "anthropic",
            "anthropic_foundry",
            "openai",
            "xai",
            "openai_foundry",
            "gemini",
            "ollama",
        ]
        assert data["server"] is False
        bedrock = {f["path"]: f for f in data["services"][0]["fields"]}
        modes = {o["value"]: o["when"] for o in bedrock["auth.mode"]["options"]}
        assert set(modes) == {
            "credential_chain",
            "access_keys",
            "short_term_api_key",
            "long_term_api_key",
        }
        assert modes["long_term_api_key"] == {"allow_long_term_api_keys": [True]}
        assert bedrock["region"]["required"] is True
        assert bedrock["auth.secret_access_key"]["type"] == "secret"


class TestSecretsAreWriteOnly:
    def test_a_typed_key_goes_to_the_store_and_config_holds_its_reference(
        self, client: TestClient, config_path: Path, secrets: MemorySecrets
    ) -> None:
        resp = _anthropic(client)
        assert resp.status_code == 201, resp.text
        cid = resp.json()["id"]
        assert cid == "claude-direct"
        name = f"connections.{cid}.api_key"
        assert secrets.data[name] == KEY
        (entry,) = _entries(config_path)
        assert entry["settings"]["api_key"] == f"secret://{name}"
        assert KEY not in config_path.read_text(encoding="utf-8")

    def test_no_answer_carries_the_value_only_set_or_not(self, client: TestClient) -> None:
        cid = _anthropic(client).json()["id"]
        shown = client.get(f"{API}/{cid}")
        assert shown.json()["secrets"] == {"api_key": True}
        assert "api_key" not in shown.json()["values"]
        for path in (API, f"{API}/{cid}", f"{API}/{cid}/models", f"{API}/services"):
            assert KEY not in client.get(path).text, path

    def test_a_blank_secret_keeps_the_stored_one(
        self, client: TestClient, secrets: MemorySecrets
    ) -> None:
        cid = _anthropic(client).json()["id"]
        resp = client.put(
            f"{API}/{cid}",
            json={"name": "Renamed", "values": {"base_url": "https://gateway.example.com"}},
        )
        assert resp.status_code == 200, resp.text
        shown = client.get(f"{API}/{cid}").json()
        assert shown["secrets"] == {"api_key": True}
        assert shown["values"]["base_url"] == "https://gateway.example.com"
        assert secrets.data[f"connections.{cid}.api_key"] == KEY

    def test_a_secret_no_longer_used_is_removed(
        self, client: TestClient, secrets: MemorySecrets
    ) -> None:
        body = {
            "service": "bedrock",
            "name": "Sydney",
            "values": {"region": "ap-southeast-2", "auth.mode": "access_keys"},
            "secrets": {
                "auth.access_key_id": "AKIAEXAMPLEEXAMPLE",
                "auth.secret_access_key": "example-secret-access-key",
            },
        }
        cid = client.post(API, json=body).json()["id"]
        assert f"connections.{cid}.auth.access_key_id" in secrets.data
        client.put(
            f"{API}/{cid}",
            json={"values": {"region": "ap-southeast-2", "auth.mode": "credential_chain"}},
        )
        assert not [k for k in secrets.data if k.startswith(f"connections.{cid}.")]


class TestValidation:
    def test_conduits_refusal_is_shown_and_nothing_is_written(
        self, client: TestClient, config_path: Path, secrets: MemorySecrets
    ) -> None:
        resp = client.post(
            API,
            json={
                "service": "bedrock",
                "name": "No Region",
                "values": {"auth.mode": "long_term_api_key"},
                "secrets": {"auth.api_key": "example-bedrock-key"},
            },
        )
        assert resp.status_code == 400
        assert "region" in resp.json()["error"]
        assert secrets.data == {}
        assert not config_path.exists() or _entries(config_path) == []

    def test_long_term_bedrock_keys_only_behind_their_setting(self, client: TestClient) -> None:
        body: dict[str, Any] = {
            "service": "bedrock",
            "name": "Exploration",
            "values": {"region": "us-east-1", "auth.mode": "long_term_api_key"},
            "secrets": {"auth.api_key": "example-bedrock-key"},
        }
        refused = client.post(API, json=body)
        assert refused.status_code == 400
        assert "long-term" in refused.json()["error"]
        body["values"]["allow_long_term_api_keys"] = True
        assert client.post(API, json=body).status_code == 201

    def test_an_unknown_service_is_refused(self, client: TestClient) -> None:
        resp = client.post(API, json={"service": "stub", "name": "x"})
        assert resp.status_code == 400

    def test_fields_outside_the_service_never_reach_the_settings(
        self, client: TestClient, config_path: Path
    ) -> None:
        cid = _anthropic(client, values={"endpoint": "foundry", "evil": "x"}).json()["id"]
        (entry,) = _entries(config_path)
        assert entry["id"] == cid
        assert entry["settings"]["endpoint"] == "direct"
        assert "evil" not in entry["settings"]

    def test_residency_defaults_follow_m15(self, client: TestClient, config_path: Path) -> None:
        client.post(
            API,
            json={"service": "bedrock", "name": "Syd", "values": {"region": "ap-southeast-2"}},
        )
        _anthropic(client)
        client.post(API, json={"service": "ollama", "name": "Box", "values": {}})
        residency = {e["id"]: e["residency"] for e in _entries(config_path)}
        assert residency["syd"] == {
            "scopes": ["in_region", "local"],
            "regions": ["ap-southeast-2"],
        }
        assert residency["claude-direct"]["regions"] is None
        assert residency["box"] == {"scopes": ["local"], "regions": None}


class TestList:
    def test_rename_disable_enable_and_list(self, client: TestClient) -> None:
        cid = _anthropic(client).json()["id"]
        assert client.post(f"{API}/{cid}/rename", json={"name": "Claude"}).status_code == 200
        assert client.post(f"{API}/{cid}/enabled", json={"enabled": False}).status_code == 200
        (item,) = client.get(API).json()["connections"]
        assert item["name"] == "Claude" and item["status"] == "disabled"
        assert item["service_label"] == "Anthropic"
        assert "metered" not in item and "assignments" not in item
        client.post(f"{API}/{cid}/enabled", json={"enabled": True})
        (item,) = client.get(API).json()["connections"]
        assert item["status"] == "running"
        assert client.app.state.connections.connection(cid).name == "Claude"  # type: ignore[attr-defined]

    def test_two_connections_of_one_service(self, client: TestClient) -> None:
        assert _anthropic(client).json()["id"] == "claude-direct"
        assert _anthropic(client).json()["id"] == "claude-direct-2"


class TestDelete:
    def test_refused_while_an_action_uses_it_naming_the_action(
        self, client: TestClient, secrets: MemorySecrets
    ) -> None:
        _stub(client)
        action = _call(
            client,
            autonomous_actions.create_action,
            "Morning brief",
            "Summarise the news",
            "lab/stub-echo",
            LOCAL,
        )
        refused = client.delete(f"{API}/lab")
        assert refused.status_code == 409
        assert "Morning brief" in refused.json()["error"]
        assert refused.json()["actions"][0]["name"] == "Morning brief"
        assert client.get(f"{API}/lab").status_code == 200
        _call(client, autonomous_actions.delete_action, action, LOCAL)
        assert client.delete(f"{API}/lab").status_code == 200
        assert client.get(f"{API}/lab").status_code == 404

    def test_delete_removes_its_secrets(self, client: TestClient, secrets: MemorySecrets) -> None:
        cid = _anthropic(client).json()["id"]
        assert client.delete(f"{API}/{cid}").status_code == 200
        assert secrets.data == {}


class TestModels:
    def test_discovery_marks_reachable_and_lists_undeclared(self, client: TestClient) -> None:
        _stub(client)
        view = client.post(f"{API}/lab/discover").json()
        rows = {m["model_id"]: m for m in view["models"]}
        assert rows["stub-echo"]["reachable"] is True
        assert view["undeclared"] == [{"model_id": "pulled:1b", "label": "local"}]
        (item,) = client.get(API).json()["connections"]
        assert item["discovery"]["summary"] == "1 model reachable, 1 not in the catalogue"

    def test_declaring_an_undeclared_model_adds_it(self, client: TestClient) -> None:
        _stub(client)
        client.post(f"{API}/lab/discover")
        resp = client.post(
            f"{API}/lab/overlays",
            json={
                "model_id": "pulled:1b",
                "kind": "chat",
                "context_window": 32768,
                "output_limit": 4096,
                "tools": True,
                "residency_scope": "local",
            },
        )
        assert resp.status_code == 200, resp.text
        rows = {m["model_id"]: m for m in resp.json()["models"]}
        assert rows["pulled:1b"]["overlay"] is True and rows["pulled:1b"]["tools"] is True
        assert rows["pulled:1b"]["reachable"] is True
        assert resp.json()["undeclared"] == []

    def test_conduit_refuses_a_bad_entry(self, client: TestClient) -> None:
        _stub(client)
        resp = client.post(
            f"{API}/lab/overlays",
            json={"model_id": "x", "context_window": 10, "residency_scope": "in_region"},
        )
        assert resp.status_code == 400
        assert "conduit refused" in resp.json()["error"] or "Output limit" in resp.json()["error"]

    def test_the_first_choice_curates_without_dropping_models(self, client: TestClient) -> None:
        _stub(client)
        _call(client, save_overlay, OverlayRow("stub", "model-b", 8192, "local", output_limit=1024))
        _refresh(client)
        resp = client.put(f"{API}/lab/models", json={"model_id": "model-b", "tier": "higher"})
        assert resp.status_code == 200, resp.text
        rows = {m["model_id"]: m for m in resp.json()["models"]}
        assert resp.json()["curated"] is True
        assert rows["stub-echo"]["enabled"] and rows["model-b"]["tier"] == "higher"
        offered = {m["id"]: m["tier"] for m in client.app.state.connections.listed_models("lab")}  # type: ignore[attr-defined]
        assert offered == {"lab/model-b": "higher", "lab/stub-echo": "standard"}
        client.put(f"{API}/lab/models", json={"model_id": "stub-echo", "enabled": False})
        offered = {m["id"] for m in client.app.state.connections.listed_models("lab")}  # type: ignore[attr-defined]
        assert offered == {"lab/model-b"}

    def test_residency_refusal_is_shown_with_conduits_reason(self, client: TestClient) -> None:
        _stub(client)
        _call(client, save_overlay, OverlayRow("stub", "far", 8192, "global", output_limit=1024))
        _refresh(client)
        rows = {m["model_id"]: m for m in client.get(f"{API}/lab/models").json()["models"]}
        assert "scope global is not allowed" in rows["far"]["refused"]
        assert rows["stub-echo"]["refused"] is None

    def test_an_unknown_model_is_refused(self, client: TestClient) -> None:
        _stub(client)
        assert client.put(f"{API}/lab/models", json={"model_id": "nope"}).status_code == 404
        resp = client.put(f"{API}/lab/models", json={"model_id": "stub-echo", "tier": "top"})
        assert resp.status_code == 400


class TestPersonalMode:
    def test_metering_and_assignment_are_server_only(self, client: TestClient) -> None:
        _stub(client)
        for method, path in (
            ("get", f"{API}/lab/metering"),
            ("put", f"{API}/lab/metering"),
            ("get", f"{API}/lab/assignments"),
            ("put", f"{API}/lab/assignments"),
            ("delete", f"{API}/lab/assignments?role=user"),
        ):
            kwargs: dict[str, Any] = {"json": {}} if method == "put" else {}
            assert getattr(client, method)(path, **kwargs).status_code == 404, path

    def test_the_page_lives_in_settings(self, client: TestClient) -> None:
        resp = client.get("/admin/connections", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings#section-connections"
        page = client.get("/settings")
        assert 'id="section-connections"' in page.text
        assert "/static/js/connections.js" in page.text


class TestRequestGuards:
    def test_state_changes_need_the_csrf_token(self, client: TestClient) -> None:
        _stub(client)
        token = client.headers.pop(CSRF_HEADER)
        try:
            for method, path in (
                ("post", API),
                ("put", f"{API}/lab"),
                ("delete", f"{API}/lab"),
                ("post", f"{API}/lab/discover"),
                ("put", f"{API}/lab/models"),
            ):
                kwargs: dict[str, Any] = {} if method == "delete" else {"json": {}}
                assert getattr(client, method)(path, **kwargs).status_code == 403, path
        finally:
            client.headers[CSRF_HEADER] = token
        assert client.get(f"{API}/lab").status_code == 200

    def test_state_changes_need_the_instances_origin(self, client: TestClient) -> None:
        _stub(client)
        resp = client.delete(f"{API}/lab", headers={"Origin": "https://elsewhere.example.com"})
        assert resp.status_code == 403
        assert client.get(f"{API}/lab").status_code == 200
