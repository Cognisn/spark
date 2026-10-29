"""Start-up wiring in spark.core.application: secret database URLs, migration, validation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from konfig.secrets.encrypted_file import EncryptedFileBackend
from konfig.secrets.secrets import Secrets
from konfig.settings.settings import Settings

from spark import mode
from spark.core import application
from spark.instance_settings.startup import StartupConfigError

DB_URL = "postgresql://spark:pw-app-dev-only@db.example.com:5432/spark"


@pytest.fixture(autouse=True)
def _reset_mode() -> Iterator[None]:
    yield
    mode.reset()


class Ctx:
    def __init__(self, config: Path, secrets: Secrets) -> None:
        self.settings = Settings(config_file=config)
        self.secrets = secrets


def _ctx(tmp_path: Path, config: dict[str, Any], stored: dict[str, str]) -> tuple[Ctx, Path]:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8", newline="\n")
    backend = EncryptedFileBackend(tmp_path / "secrets.enc", master_key="mk-app-dev-only")
    for key, value in stored.items():
        backend.set(key, value)
    return Ctx(path, Secrets(backend=backend)), path


class TestConfigureRuntime:
    def test_secret_database_url_resolved(self, tmp_path: Path) -> None:
        ctx, _ = _ctx(
            tmp_path,
            {"mode": "server", "database": {"url": "secret://database_url"}},
            {"database_url": DB_URL},
        )
        runtime = application.configure_runtime(ctx.settings, ctx.secrets)
        assert runtime.database.url == DB_URL

    def test_without_secrets_behaves_as_before(self, tmp_path: Path) -> None:
        ctx, _ = _ctx(tmp_path, {"mode": "server", "database": {"url": DB_URL}}, {})
        assert application.configure_runtime(ctx.settings).database.url == DB_URL


class TestValidateStartup:
    def test_legacy_url_secret_migrated_then_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 0.2 settings page stored database.url as the secret 'url' (issue fixed here)."""
        monkeypatch.setenv("KONFIG_MASTER_KEY", "mk-app-dev-only")
        ctx, path = _ctx(
            tmp_path,
            {
                "mode": "server",
                "database": {"url": "secret://url"},
                "secrets": {"backend": "encrypted_file"},
            },
            {"url": DB_URL},
        )
        runtime = application.validate_startup(ctx, path)  # type: ignore[arg-type]
        assert runtime.database.url == DB_URL
        assert mode.current().database.url == DB_URL
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["database"]["url"] == (
            "secret://database_url"
        )
        assert ctx.secrets.get("database_url") == DB_URL
        assert ctx.secrets.get("url") is None
        assert DB_URL not in path.read_text(encoding="utf-8")

    def test_server_mode_on_the_keychain_refused(self, tmp_path: Path) -> None:
        ctx, path = _ctx(tmp_path, {"mode": "server", "database": {"url": DB_URL}}, {})
        with pytest.raises(StartupConfigError, match="secrets.backend"):
            application.validate_startup(ctx, path)  # type: ignore[arg-type]

    def test_personal_defaults(self, tmp_path: Path) -> None:
        ctx, path = _ctx(tmp_path, {"mode": "personal"}, {})
        runtime = application.validate_startup(ctx, path)  # type: ignore[arg-type]
        assert runtime.mode is mode.Mode.PERSONAL
