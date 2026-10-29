"""Start-up configuration through konfig, validated with clear messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spark.instance_settings import startup
from spark.instance_settings.startup import StartupConfigError
from spark.mode import Mode, ModeConfigError

DB_URL = "postgresql://spark:pw-startup-dev-only@db.example.com:5432/spark"
ARN = "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark-dev-only"
SQLITE = Path("spark-test.db")


class Konfig:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class Store:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data or {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def has(self, key: str) -> bool:
        return key in self.data


def _server(**extra: Any) -> dict[str, Any]:
    return {
        "mode": "server",
        "database.url": "secret://database_url",
        "secrets.backend": "encrypted_file",
        "server.public_base_url": "https://spark.example.com",
        **extra,
    }


def _validate(values: dict[str, Any], store: Store | None = None, **env: str) -> Any:
    return startup.validate(
        Konfig(values),
        store or Store({"database_url": DB_URL}),
        default_sqlite_path=SQLITE,
        environ=env,
    )


class TestPersonal:
    def test_defaults_are_valid(self) -> None:
        config = _validate({}, Store())
        assert config.runtime.mode is Mode.PERSONAL
        assert config.runtime.database.sqlite_path == SQLITE
        assert config.warnings == ()

    def test_personal_mode_keeps_the_keychain(self) -> None:
        assert _validate({}, Store()).secrets_backend == "keyring"


class TestDatabaseUrl:
    def test_resolved_from_the_secrets_store(self) -> None:
        config = _validate(_server(), KONFIG_MASTER_KEY="mk-dev-only")
        assert config.runtime.database.url == DB_URL

    def test_plain_url_still_accepted(self) -> None:
        config = _validate(_server(**{"database.url": DB_URL}), KONFIG_MASTER_KEY="mk-dev-only")
        assert config.runtime.database.url == DB_URL

    def test_missing_secret_named_clearly(self) -> None:
        with pytest.raises(StartupConfigError, match="database_url.*not set"):
            _validate(_server(), Store(), KONFIG_MASTER_KEY="mk-dev-only")

    def test_unmigrated_legacy_reference_named_clearly(self) -> None:
        with pytest.raises(StartupConfigError, match="'url'"):
            _validate(_server(**{"database.url": "secret://url"}), Store(), KONFIG_MASTER_KEY="mk")

    def test_message_never_contains_the_url(self) -> None:
        bad = "mysql://spark:pw-startup-dev-only@db.example.com/spark"
        with pytest.raises(ModeConfigError) as info:
            _validate(_server(), Store({"database_url": bad}), KONFIG_MASTER_KEY="mk-dev-only")
        assert "pw-startup-dev-only" not in str(info.value)

    def test_server_without_a_url(self) -> None:
        values = _server()
        del values["database.url"]
        with pytest.raises(ModeConfigError, match="database.url"):
            _validate(values, KONFIG_MASTER_KEY="mk-dev-only")

    def test_errors_are_mode_config_errors(self) -> None:
        assert issubclass(StartupConfigError, ModeConfigError)


class TestServerSecretsBackend:
    def test_encrypted_store_with_master_key(self) -> None:
        assert _validate(_server(), KONFIG_MASTER_KEY="mk-dev-only").secrets_backend == (
            "encrypted_file"
        )

    def test_master_key_through_settings(self) -> None:
        config = _validate(_server(**{"secrets.master_key": "mk-dev-only"}))
        assert config.secrets_backend == "encrypted_file"

    def test_encrypted_store_without_master_key_refused(self) -> None:
        with pytest.raises(StartupConfigError, match="KONFIG_MASTER_KEY"):
            _validate(_server())

    def test_aws_secrets_manager_backend(self) -> None:
        config = _validate(_server(**{"secrets.backend": "aws_secrets_manager"}))
        assert config.secrets_backend == "aws_secrets_manager"

    def test_aws_bundle_from_the_environment_wins(self) -> None:
        values = _server()
        del values["secrets.backend"]
        config = _validate(values, KONFIG_AWS_SECRETS_MANAGER=ARN)
        assert config.secrets_backend == "aws_bundle"

    def test_malformed_aws_arn_refused(self) -> None:
        with pytest.raises(StartupConfigError, match="KONFIG_AWS_SECRETS_MANAGER"):
            _validate(_server(), KONFIG_AWS_SECRETS_MANAGER="not-an-arn")

    @pytest.mark.parametrize("backend", [None, "keyring", "sticky-notes"])
    def test_keyring_or_unknown_backend_refused_in_server_mode(self, backend: str | None) -> None:
        values = _server(**{"secrets.backend": backend})
        with pytest.raises(StartupConfigError, match="secrets.backend"):
            _validate(values, KONFIG_MASTER_KEY="mk-dev-only")


class TestPublicBaseUrlAndWarden:
    def test_invalid_public_base_url_refused(self) -> None:
        with pytest.raises(StartupConfigError, match="server.public_base_url"):
            _validate(
                _server(**{"server.public_base_url": "spark.example.com"}),
                KONFIG_MASTER_KEY="mk-dev-only",
            )

    def test_missing_public_base_url_is_a_warning_in_server_mode(self) -> None:
        values = _server()
        del values["server.public_base_url"]
        config = _validate(values, KONFIG_MASTER_KEY="mk-dev-only")
        assert any("server.public_base_url" in w for w in config.warnings)

    def test_public_base_url_returned(self) -> None:
        config = _validate(_server(), KONFIG_MASTER_KEY="mk-dev-only")
        assert config.public_base_url == "https://spark.example.com"

    def test_warden_placeholders_validated_when_set(self) -> None:
        with pytest.raises(StartupConfigError, match="warden.url"):
            _validate(_server(**{"warden.url": "warden"}), KONFIG_MASTER_KEY="mk-dev-only")
        config = _validate(
            _server(**{"warden.url": "https://warden.example.com", "warden.client_id": "spark"}),
            KONFIG_MASTER_KEY="mk-dev-only",
        )
        assert config.warden == {"url": "https://warden.example.com", "client_id": "spark"}


class TestDatabaseView:
    def test_other_keys_pass_through(self) -> None:
        view = startup.database_view(Konfig({"mode": "server", "x": 1}), Store())
        assert view.get("mode") == "server"
        assert view.get("x") == 1
        assert view.get("missing", "d") == "d"
