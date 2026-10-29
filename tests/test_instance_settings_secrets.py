"""Secrets: written only through konfig, write-only, and the 0.2 names migrated."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from spark.instance_settings import audit, secrets
from spark.instance_settings.audit import SettingChange
from spark.instance_settings.registry import InvalidSettingError, SettingsError
from spark.instance_settings.secrets import SecretsWriter

KEY = "sk-ant-secrets-test-dev-only"
DB_URL = "postgresql://spark:pw-dev-only@db.example.com:5432/spark"


class MemorySecrets:
    """An in-memory stand-in for konfig's Secrets (the keychain in personal mode)."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self.data

    def resolve_uri(self, value: str) -> str | None:
        if value.startswith("secret://"):
            return self.data.get(value[len("secret://") :])
        return value


class Konfig:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def _no_hook() -> Iterator[None]:
    yield
    audit.set_audit_hook(None)


class TestWriter:
    def test_set_stores_under_the_grouped_name(self) -> None:
        store = MemorySecrets()
        SecretsWriter(store, Konfig(), server=True).set(
            "providers.anthropic.api_key", KEY, actor="admin"
        )
        assert store.data == {"providers_anthropic_api_key": KEY}

    def test_database_url_is_no_longer_stored_as_url(self) -> None:
        store = MemorySecrets()
        SecretsWriter(store, Konfig(), server=True).set("database.url", DB_URL, actor="admin")
        assert "url" not in store.data
        assert store.data["database_url"] == DB_URL

    def test_refuses_non_secret_and_unknown_keys(self) -> None:
        writer = SecretsWriter(MemorySecrets(), Konfig(), server=True)
        with pytest.raises(SettingsError):
            writer.set("providers.anthropic.enabled", "true", actor="admin")
        with pytest.raises(SettingsError):
            writer.set("no.such.secret", KEY, actor="admin")

    def test_refuses_empty_and_non_text_values(self) -> None:
        writer = SecretsWriter(MemorySecrets(), Konfig(), server=True)
        for bad in ("", "   ", 12):
            with pytest.raises(InvalidSettingError):
                writer.set("providers.anthropic.api_key", bad, actor="admin")  # type: ignore[arg-type]

    def test_clear(self) -> None:
        store = MemorySecrets({"providers_anthropic_api_key": KEY})
        writer = SecretsWriter(store, Konfig(), server=True)
        writer.clear("providers.anthropic.api_key", actor="admin")
        assert store.data == {}
        assert not writer.is_set("providers.anthropic.api_key")

    def test_status_says_set_or_not_set_and_never_the_value(self) -> None:
        store = MemorySecrets({"providers_anthropic_api_key": KEY})
        status = SecretsWriter(store, Konfig(), server=True).status()
        assert status["providers.anthropic.api_key"] is True
        assert status["providers.xai.api_key"] is False
        assert set(status.values()) <= {True, False}
        assert KEY not in json.dumps(status)

    def test_audit_event_marks_secret_and_has_no_value(self) -> None:
        events: list[SettingChange] = []
        audit.set_audit_hook(events.append)
        writer = SecretsWriter(MemorySecrets(), Konfig(), server=True)
        writer.set("providers.anthropic.api_key", KEY, actor="admin")
        writer.clear("providers.anthropic.api_key", actor="admin")
        assert [(e.key, e.action, e.secret) for e in events] == [
            ("providers.anthropic.api_key", "set", True),
            ("providers.anthropic.api_key", "clear", True),
        ]
        assert KEY not in repr(events)

    def test_resolve_is_for_server_side_use(self) -> None:
        store = MemorySecrets({"providers_anthropic_api_key": KEY})
        writer = SecretsWriter(store, Konfig(), server=True)
        assert writer.resolve("providers.anthropic.api_key") == KEY
        assert writer.resolve("providers.xai.api_key") == ""


class TestPersonalMode:
    def test_is_set_follows_the_config_reference(self) -> None:
        store = MemorySecrets({"providers_anthropic_api_key": KEY})
        konfig = Konfig({"providers.anthropic.api_key": "secret://providers_anthropic_api_key"})
        writer = SecretsWriter(store, konfig, server=False)
        assert writer.is_set("providers.anthropic.api_key")

    def test_reference_to_a_missing_secret_is_not_set(self) -> None:
        konfig = Konfig({"providers.anthropic.api_key": "secret://anthropic_api_key"})
        writer = SecretsWriter(MemorySecrets(), konfig, server=False)
        assert not writer.is_set("providers.anthropic.api_key")

    def test_a_plain_value_in_the_config_counts_as_set(self) -> None:
        konfig = Konfig({"embedded_tools.web.brave_api_key": "brave-dev-only"})
        writer = SecretsWriter(MemorySecrets(), konfig, server=False)
        assert writer.is_set("embedded_tools.web.brave_api_key")
        assert writer.resolve("embedded_tools.web.brave_api_key") == "brave-dev-only"

    def test_resolve_follows_the_reference(self) -> None:
        store = MemorySecrets({"elsewhere": KEY})
        konfig = Konfig({"providers.anthropic.api_key": "secret://elsewhere"})
        writer = SecretsWriter(store, konfig, server=False)
        assert writer.resolve("providers.anthropic.api_key") == KEY

    def test_reference_text(self) -> None:
        assert secrets.reference("database.url") == "secret://database_url"


class TestMigration:
    def test_legacy_name_is_moved_and_the_reference_rewritten(self) -> None:
        store = MemorySecrets({"url": DB_URL, "anthropic_api_key": KEY})
        raw = {
            "database": {"url": "secret://url"},
            "providers": {"anthropic": {"api_key": "secret://anthropic_api_key"}},
        }
        changed = secrets.migrate_config(raw, store)
        assert sorted(changed) == ["database.url", "providers.anthropic.api_key"]
        assert raw["database"]["url"] == "secret://database_url"
        assert raw["providers"]["anthropic"]["api_key"] == "secret://providers_anthropic_api_key"
        assert store.data == {"database_url": DB_URL, "providers_anthropic_api_key": KEY}

    def test_plain_secret_values_move_into_the_store(self) -> None:
        store = MemorySecrets()
        raw = {"embedded_tools": {"web": {"brave_api_key": "brave-dev-only"}}}
        assert secrets.migrate_config(raw, store) == ["embedded_tools.web.brave_api_key"]
        assert raw["embedded_tools"]["web"]["brave_api_key"] == (
            "secret://embedded_tools_web_brave_api_key"
        )
        assert store.data == {"embedded_tools_web_brave_api_key": "brave-dev-only"}

    def test_an_existing_new_entry_is_kept(self) -> None:
        store = MemorySecrets({"url": "postgresql://old-dev-only", "database_url": DB_URL})
        raw = {"database": {"url": "secret://url"}}
        secrets.migrate_config(raw, store)
        assert store.data["database_url"] == DB_URL
        assert raw["database"]["url"] == "secret://database_url"

    def test_reference_without_a_stored_secret_is_rewritten(self) -> None:
        raw = {"providers": {"xai": {"api_key": "secret://xai_api_key"}}}
        store = MemorySecrets()
        assert secrets.migrate_config(raw, store) == ["providers.xai.api_key"]
        assert raw["providers"]["xai"]["api_key"] == "secret://providers_xai_api_key"
        assert store.data == {}

    def test_current_names_empty_values_and_other_references_untouched(self) -> None:
        raw = {
            "database": {"url": "secret://database_url"},
            "embedded_tools": {"email": {"password": ""}},
            "mcp": {"servers": [{"env": {"API_KEY": "secret://example_key"}}]},
        }
        before = json.dumps(raw, sort_keys=True)
        assert secrets.migrate_config(raw, MemorySecrets()) == []
        assert json.dumps(raw, sort_keys=True) == before

    def test_idempotent(self) -> None:
        store = MemorySecrets({"url": DB_URL})
        raw = {"database": {"url": "secret://url"}}
        secrets.migrate_config(raw, store)
        assert secrets.migrate_config(raw, store) == []

    def test_migrate_config_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "mode: server\ndatabase:\n  url: secret://url\n", encoding="utf-8", newline="\n"
        )
        store = MemorySecrets({"url": DB_URL})
        assert secrets.migrate_config_file(path, store) == ["database.url"]
        text = path.read_text(encoding="utf-8")
        assert DB_URL not in text
        assert yaml.safe_load(text) == {
            "mode": "server",
            "database": {"url": "secret://database_url"},
        }
        assert b"\r\n" not in path.read_bytes()

    def test_migrate_config_file_leaves_an_unchanged_file_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("# comment kept\nmode: personal\n", encoding="utf-8", newline="\n")
        assert secrets.migrate_config_file(path, MemorySecrets()) == []
        assert path.read_text(encoding="utf-8") == "# comment kept\nmode: personal\n"

    def test_missing_config_file(self, tmp_path: Path) -> None:
        assert secrets.migrate_config_file(tmp_path / "absent.yaml", MemorySecrets()) == []


class TestThroughKonfigBackends:
    def test_encrypted_store_holds_the_secret_encrypted(self, tmp_path: Path) -> None:
        from konfig.secrets.encrypted_file import EncryptedFileBackend
        from konfig.secrets.secrets import Secrets

        path = tmp_path / "secrets.enc"
        backend = EncryptedFileBackend(path, master_key="master-key-test-dev-only")
        writer = SecretsWriter(Secrets(backend=backend), Konfig(), server=True)
        writer.set("providers.anthropic.api_key", KEY, actor="admin")
        assert KEY.encode() not in path.read_bytes()
        reopened = EncryptedFileBackend(path, master_key="master-key-test-dev-only")
        assert reopened.get("providers_anthropic_api_key") == KEY

    def test_aws_bundle_backend_receives_the_write(self) -> None:
        from konfig.secrets.aws_bundle_backend import AWSSecretsBundleBackend
        from konfig.secrets.secrets import Secrets

        class FakeClient:
            class exceptions:  # noqa: N801 - mirrors boto3
                class ResourceNotFoundException(Exception):
                    pass

            def __init__(self) -> None:
                self.secret = "{}"

            def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803
                return {"SecretString": self.secret}

            def put_secret_value(self, SecretId: str, SecretString: str) -> None:  # noqa: N803
                self.secret = SecretString

        client = FakeClient()
        arn = "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark-dev-only"
        backend = AWSSecretsBundleBackend(arn=arn, client=client)
        writer = SecretsWriter(Secrets(backend=backend), Konfig(), server=True)
        writer.set("database.url", DB_URL, actor="admin")
        assert json.loads(client.secret) == {"database_url": DB_URL}
        assert writer.is_set("database.url")
