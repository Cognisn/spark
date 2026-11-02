"""Every process configures its runtime first, with its secrets (D13).

``spark web`` (validate_startup), ``spark migrate`` and ``spark worker``
(configure_runtime) each set the mode and resolve a ``secret://``
``database.url`` through konfig's secrets before any database or tool work.
The configuration and secrets are synthetic; nothing connects to a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from konfig.secrets.encrypted_file import EncryptedFileBackend
from konfig.secrets.secrets import Secrets
from konfig.settings.settings import Settings

from spark import mode, worker
from spark.core import application
from spark.mode import Mode, RuntimeSettings

DB_URL = "postgresql://spark:pw-start-dev-only@db.example.com:5432/spark"


@pytest.fixture(autouse=True)
def _reset_mode() -> Iterator[None]:
    mode.reset()
    yield
    mode.reset()


class _Context:
    """konfig's AppContext with a real settings file and an encrypted secrets store."""

    config: Path
    secrets_file: Path

    def __init__(self, **_: Any) -> None:
        self.settings = Settings(config_file=self.config)
        self.secrets = Secrets(
            backend=EncryptedFileBackend(self.secrets_file, master_key="mk-start-dev-only")
        )

    async def __aenter__(self) -> _Context:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def secret_url_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> type[_Context]:
    """A server configuration whose database.url is ``secret://database_url``."""
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"mode": "server", "database": {"url": "secret://database_url"}}),
        encoding="utf-8",
        newline="\n",
    )
    secrets_file = tmp_path / "secrets.enc"
    EncryptedFileBackend(secrets_file, master_key="mk-start-dev-only").set("database_url", DB_URL)

    class Context(_Context):
        pass

    Context.config = config
    Context.secrets_file = secrets_file
    monkeypatch.setattr(application, "_get_config_path", lambda: config)
    monkeypatch.setattr(application, "_get_data_path", lambda: tmp_path / "data")
    return Context


class TestWorker:
    def test_a_secret_database_url_resolves(
        self, secret_url_config: type[_Context], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("konfig.AppContext", secret_url_config)
        runtime = worker._load_runtime()
        assert runtime.mode is Mode.SERVER
        assert runtime.database.url == DB_URL

    def test_the_runtime_is_configured_before_the_body_runs(
        self, secret_url_config: type[_Context], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker's duties (database, tools) start in the body: the mode is set by then."""
        monkeypatch.setattr("konfig.AppContext", secret_url_config)
        seen: list[RuntimeSettings] = []

        def body(app: Any) -> None:
            seen.append(mode.current())

        runtime, _ = worker._with_app(body)
        assert seen == [runtime]
        assert seen[0].database.url == DB_URL


class TestMigrate:
    def test_a_secret_database_url_resolves_before_migrating(
        self, secret_url_config: type[_Context], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(application, "AppContext", secret_url_config)
        migrated: list[tuple[RuntimeSettings, RuntimeSettings]] = []

        def run_migrations(runtime: RuntimeSettings) -> dict[str, str | None]:
            migrated.append((runtime, mode.current()))
            return {"spark": "head"}

        monkeypatch.setattr(application, "run_migrations", run_migrations)
        assert application.run_migrate() == 0
        [(runtime, configured)] = migrated
        assert runtime.database.url == DB_URL
        assert configured == runtime


class TestWeb:
    def test_start_up_is_validated_before_the_app_is_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[str] = []

        class Context:
            def __init__(self, **_: Any) -> None:
                self.settings = object()
                self.secrets = object()

            async def __aenter__(self) -> Context:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

        def validate(ctx: Any, config_path: Path) -> None:
            assert ctx.secrets is not None
            calls.append("validate_startup")

        async def serve(ctx: Any, *, first_run: bool = False) -> None:
            calls.append("create_and_serve")

        monkeypatch.setattr(application, "_get_config_path", lambda: tmp_path / "config.yaml")
        monkeypatch.setattr(application, "AppContext", Context)
        monkeypatch.setattr(application, "validate_startup", validate)
        monkeypatch.setattr(application, "_log_startup_paths", lambda ctx, path: None)
        monkeypatch.setattr("spark.web.server.create_and_serve", serve)
        application.run()
        assert calls == ["validate_startup", "create_and_serve"]

    def test_validate_startup_resolves_a_secret_database_url(
        self, secret_url_config: type[_Context], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KONFIG_MASTER_KEY", "mk-start-dev-only")
        ctx = secret_url_config()
        data = yaml.safe_load(ctx.config.read_text(encoding="utf-8"))
        data["secrets"] = {"backend": "encrypted_file"}
        data["server"] = {"public_base_url": "https://spark.example.com"}
        ctx.config.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
        ctx.settings.reload()
        runtime = application.validate_startup(ctx, ctx.config)  # type: ignore[arg-type]
        assert runtime.database.url == DB_URL
        assert mode.current().database.url == DB_URL
