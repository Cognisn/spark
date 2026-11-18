"""SPARK on AWS: konfig's settings document and secrets bundle, against a stubbed service.

The deployment (docs/deployment/aws.md) keeps SPARK's start-up settings in one
read-only Secrets Manager document (``KONFIG_AWS_SETTINGS``) and its secrets in
one bundle (``KONFIG_AWS_SECRETS_MANAGER``), with no master key and
``KONFIG_AWS_SEED=false``. Each task names its own database login through
``SPARK__DATABASE__URL=secret://database_url_<login>``. These tests run konfig's
real ``AppContext`` (its layers, its bundle backend) over an in-memory Secrets
Manager, and SPARK's real start-up validation for the web, migrate and worker
entry points.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from konfig import AppContext

from spark import files, mode
from spark.core import application
from spark.instance_settings import startup
from tests.aws_helpers import (
    BUNDLE_ARN,
    OWNER_URL,
    PUBLIC,
    SETTINGS_ARN,
    WEB_URL,
    WORKER_URL,
    FakeSecretsManager,
    environment_bundle,
    environment_settings_document,
    install_secrets_manager,
)


@pytest.fixture(autouse=True)
def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith(("SPARK__", "KONFIG_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KONFIG_AWS_SEED", "false")
    monkeypatch.setenv("SPARK__LOGGING__LOG_DIR", str(tmp_path / "logs"))
    yield
    mode.reset()
    files.reset()


def _context(tmp_path: Path, config: dict[str, Any] | None = None) -> AppContext:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config or {}), encoding="utf-8", newline="\n")
    return AppContext(
        name="Spark",
        version="0.0.0",
        config_file=str(path),
        system_config_file=str(tmp_path / "none" / "config.yaml"),
        defaults=application._default_settings(),
        env_prefix="SPARK",
    )


def _web(ctx: AppContext, tmp_path: Path) -> mode.RuntimeSettings:
    return application.validate_startup(ctx, tmp_path / "config.yaml")


def _migrate(ctx: AppContext) -> mode.RuntimeSettings:
    return application.configure_runtime(ctx.settings, ctx.secrets)


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> FakeSecretsManager:
    return install_secrets_manager(
        monkeypatch,
        settings=environment_settings_document(),
        bundle=environment_bundle(),
    )


# -- Starting with the settings document and the bundle, no master key -------------------


def test_a_server_starts_with_the_document_and_the_bundle_and_no_master_key(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    with _context(tmp_path) as ctx:
        runtime = _web(ctx, tmp_path)
        assert runtime.mode is mode.Mode.SERVER
        assert runtime.database.url == WEB_URL
        assert startup.secrets_backend(ctx.settings, os.environ, True) == "aws_bundle"
        assert type(ctx.secrets.backend).__name__ == "AWSSecretsBundleBackend"
    assert "KONFIG_MASTER_KEY" not in os.environ


# -- SPARK__DATABASE__URL names the task's own login ------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("database_url_owner", OWNER_URL),
        ("database_url_web", WEB_URL),
        ("database_url_worker", WORKER_URL),
    ],
)
@pytest.mark.parametrize("entry", ["web", "migrate"])
def test_the_task_environment_selects_its_own_database_login(
    aws: FakeSecretsManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    name: str,
    expected: str,
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", f"secret://{name}")
    with _context(tmp_path) as ctx:
        runtime = _web(ctx, tmp_path) if entry == "web" else _migrate(ctx)
    assert runtime.mode is mode.Mode.SERVER
    assert runtime.database.url == expected


def test_the_worker_entry_point_resolves_the_reference_too(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spark import worker

    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_worker")
    monkeypatch.setattr(application, "_get_config_path", lambda: tmp_path / "config.yaml")
    runtime = worker._load_runtime()
    assert runtime.database.url == WORKER_URL
    worker.check_runtime(runtime)  # server mode with PostgreSQL: not refused


def test_the_environment_url_wins_over_database_url_elsewhere(
    aws: FakeSecretsManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A database.url in config.yaml and in the settings document must both lose.
    document = environment_settings_document()
    document["database"] = {"url": "postgresql://doc:CHANGEME@document.example.com/spark"}
    aws.secrets[SETTINGS_ARN] = json.dumps(document)
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    config = {"database": {"url": "postgresql://file:CHANGEME@file.example.com/spark"}}
    with _context(tmp_path, config) as ctx:
        assert _web(ctx, tmp_path).database.url == WEB_URL
        assert _migrate(ctx).database.url == WEB_URL


def test_a_reference_to_a_missing_secret_is_refused_without_a_value(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_nobody")
    with _context(tmp_path) as ctx, pytest.raises(startup.StartupConfigError) as refused:
        _web(ctx, tmp_path)
    text = str(refused.value)
    assert "database_url_nobody" in text
    assert "CHANGEME" not in text


# -- Every key the settings document holds is honoured ------------------------------------


def test_every_key_in_the_settings_document_is_honoured(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    with _context(tmp_path) as ctx:
        # Read through konfig as SPARK reads each key.
        assert ctx.settings.get("mode") == "server"
        assert ctx.settings.get("server.public_base_url") == PUBLIC
        assert ctx.settings.get("files.root") == "/var/lib/spark-files"
        assert ctx.settings.get("logging.level") == "WARNING"
        assert ctx.settings.get("logging.format") == "json"
        # And as start-up acts on each.
        runtime = _web(ctx, tmp_path)
        assert runtime.mode is mode.Mode.SERVER
        config = startup.validate(
            ctx.settings,
            ctx.secrets,
            default_sqlite_path=tmp_path / "unused.db",
            environ=os.environ,
        )
        assert config.public_base_url == PUBLIC
        assert str(files.file_store().root).replace("\\", "/").endswith("/var/lib/spark-files")
        assert logging.getLogger().level == logging.WARNING


def test_the_document_beats_config_yaml_and_the_defaults(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    config = {
        "mode": "personal",
        "server": {"public_base_url": "https://file.example.com"},
        "files": {"root": "/from/the/file"},
        "logging": {"level": "DEBUG", "format": "text"},
    }
    with _context(tmp_path, config) as ctx:
        assert ctx.settings.get("mode") == "server"
        assert ctx.settings.get("server.public_base_url") == PUBLIC
        assert ctx.settings.get("files.root") == "/var/lib/spark-files"
        assert ctx.settings.get("logging.level") == "WARNING"
        assert _web(ctx, tmp_path).mode is mode.Mode.SERVER


@pytest.mark.parametrize(
    ("variable", "key", "value"),
    [
        ("SPARK__MODE", "mode", "personal"),
        ("SPARK__SERVER__PUBLIC_BASE_URL", "server.public_base_url", "https://env.example.com"),
        ("SPARK__FILES__ROOT", "files.root", "/from/the/environment"),
        ("SPARK__LOGGING__LEVEL", "logging.level", "ERROR"),
        ("SPARK__LOGGING__FORMAT", "logging.format", "text"),
    ],
)
def test_a_spark_variable_beats_the_settings_document(
    aws: FakeSecretsManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    with _context(tmp_path) as ctx:
        assert ctx.settings.get(key) == value


def test_the_document_is_read_only(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    before = aws.secrets[SETTINGS_ARN]
    with _context(tmp_path) as ctx:
        _web(ctx, tmp_path)
    assert aws.secrets[SETTINGS_ARN] == before
    assert SETTINGS_ARN not in aws.writes


# -- KONFIG_AWS_SEED=false leaves a pre-filled bundle untouched ---------------------------


def test_seeding_off_leaves_the_bundle_and_the_document_untouched(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    before = dict(aws.secrets)
    with _context(tmp_path) as ctx:
        _web(ctx, tmp_path)
    assert aws.secrets == before
    assert aws.writes == []


def test_seeding_off_leaves_an_empty_bundle_and_document_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = install_secrets_manager(monkeypatch)  # both secrets empty
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    with _context(tmp_path):
        pass
    assert service.writes == []
    assert service.secrets[BUNDLE_ARN] == ""
    assert service.secrets[SETTINGS_ARN] == ""


def test_a_prefilled_bundle_is_never_overwritten_even_when_seeding_is_on(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KONFIG_AWS_SEED", "true")
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    before = dict(aws.secrets)
    with _context(tmp_path) as ctx:
        _web(ctx, tmp_path)
    assert aws.secrets == before
    assert aws.writes == []


def test_the_bundle_may_be_written_by_the_task_that_owns_it(
    aws: FakeSecretsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Saving a secret from the administration screens writes one key and keeps the rest.
    monkeypatch.setenv("SPARK__DATABASE__URL", "secret://database_url_web")
    with _context(tmp_path) as ctx:
        _web(ctx, tmp_path)
        ctx.secrets.set("voice_elevenlabs_api_key", "CHANGEME-key")
    bundle = json.loads(aws.secrets[BUNDLE_ARN])
    assert bundle["voice_elevenlabs_api_key"] == "CHANGEME-key"
    assert bundle["database_url_web"] == WEB_URL
