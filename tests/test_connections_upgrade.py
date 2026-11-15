"""The upgrade to connections (M12): personal config.yaml providers become named
connections, once; a server lists the retired settings it ignores."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conduit import LiteralSecretResolver

from spark.connections import Connection
from spark.connections.runtime import ConnectionRuntime, production_factories
from spark.connections.upgrade import (
    convert_providers,
    ignored_server_settings,
    upgrade_config_file,
)


class MemorySecrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


PROVIDERS: dict[str, Any] = {
    "anthropic": {"enabled": True, "api_key": "secret://spark.providers.anthropic.api_key"},
    "aws_bedrock": {
        "enabled": True,
        "region": "ap-southeast-2",
        "auth_method": "iam",
        "access_key": "secret://aws-access",
        "secret_key": "secret://aws-secret",
        "read_timeout": 120,
    },
    "ollama": {"enabled": True, "base_url": "http://localhost:11434"},
    "google_gemini": {"enabled": False, "api_key": ""},
    "xai": {"enabled": "true", "api_key": "xai-plain-example"},
}


@pytest.fixture
def validator() -> Any:
    runtime = ConnectionRuntime(secrets=LiteralSecretResolver({}), factories=production_factories())
    yield runtime
    runtime.close()


def test_each_configured_provider_becomes_a_valid_connection(validator: Any) -> None:
    secrets = MemorySecrets()
    converted = convert_providers(PROVIDERS, secrets)
    assert [c.id for c in converted] == ["anthropic", "aws-bedrock", "ollama", "xai"]
    for connection in converted:
        validator.validate(connection)  # conduit's own settings models accept each
    by_id = {c.id: c for c in converted}
    # A reference keeps its keychain entry as it is.
    assert by_id["anthropic"].settings["api_key"] == "secret://spark.providers.anthropic.api_key"
    bedrock = by_id["aws-bedrock"]
    assert bedrock.settings["auth"] == {
        "mode": "access_keys",
        "access_key_id": "secret://aws-access",
        "secret_access_key": "secret://aws-secret",
    }
    assert bedrock.region == "ap-southeast-2"
    assert bedrock.residency.regions == frozenset({"ap-southeast-2"})  # in-Region by default
    assert by_id["ollama"].residency.scopes == frozenset({"local"})
    assert by_id["anthropic"].residency.regions is None  # a direct API: global
    # A plain-text key moves into the keychain; the connection holds its reference.
    xai_key = by_id["xai"].settings["service"]["api_key"]
    assert xai_key == "secret://connections.xai.api_key"
    assert secrets.values == {"connections.xai.api_key": "xai-plain-example"}


def test_sso_bedrock_uses_the_credential_chain(validator: Any) -> None:
    converted = convert_providers(
        {"aws_bedrock": {"enabled": True, "region": "eu-west-2", "profile": "example-ai"}},
        MemorySecrets(),
    )
    (bedrock,) = converted
    assert bedrock.settings["auth"] == {"mode": "credential_chain", "profile": "example-ai"}
    validator.validate(bedrock)


def test_disabled_provider_with_a_key_is_kept_disabled() -> None:
    (conn,) = convert_providers(
        {"google_gemini": {"enabled": False, "api_key": "secret://gem"}}, MemorySecrets()
    )
    assert conn.id == "google-gemini" and conn.enabled is False


def test_config_file_converted_once(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"interface": {"port": 22600}, "providers": PROVIDERS}), encoding="utf-8"
    )
    secrets = MemorySecrets()
    first = upgrade_config_file(path, secrets)
    assert [c.id for c in first] == ["anthropic", "aws-bedrock", "ollama", "xai"]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "providers" not in raw and raw["interface"] == {"port": 22600}
    assert [Connection.from_record(e).id for e in raw["connections"]] == [c.id for c in first]
    assert "xai-plain-example" not in path.read_text(encoding="utf-8")
    # A second start changes nothing, even if providers were written back by hand.
    raw["providers"] = {"anthropic": {"enabled": True, "api_key": "secret://other"}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert upgrade_config_file(path, secrets) == []
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["providers"]


def test_no_providers_no_change(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("interface:\n  port: 1\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert upgrade_config_file(path, MemorySecrets()) == []
    assert path.read_text(encoding="utf-8") == before
    assert upgrade_config_file(tmp_path / "missing.yaml", MemorySecrets()) == []


@pytest.mark.asyncio
async def test_server_lists_retired_settings(spark_db: Any) -> None:
    now = "2026-11-16T00:00:00+00:00"
    for key in (
        "providers.anthropic.enabled",
        "models.offered",
        "conversation.max_tool_iterations",
    ):
        await spark_db.execute(
            "INSERT INTO instance_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (key, "true", now, "test"),
        )
    assert await ignored_server_settings(spark_db) == [
        "models.offered",
        "providers.anthropic.enabled",
    ]
