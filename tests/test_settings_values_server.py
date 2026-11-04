"""A server on PostgreSQL never runs the browser heartbeat, whatever the environment says.

The app is the real one (``create_app`` with ``attach_database`` and warden
attached) in server mode on a PostgreSQL clone, with konfig loading its
settings from a config file and monkeypatched ``SPARK__`` variables, as the
container image does.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from konfig.settings.settings import Settings

from spark import mode
from spark.core.application import _default_settings
from spark.mode import DatabaseSettings, Mode, RuntimeSettings
from spark.web.server import _heartbeat_config, attach_database, attach_identity, create_app
from tests.identity_screen_helpers import server_db  # noqa: F401  (fixture)
from tests.identity_web_helpers import PUBLIC, client_options
from tests.test_instance_settings_web import MemorySecrets

pytestmark = pytest.mark.postgres


class _EnvCtx:
    """konfig's settings as ``spark web`` loads them: config file, SPARK__ variables, defaults."""

    def __init__(self, config: Path) -> None:
        self.settings = Settings(
            config_file=config, defaults=_default_settings(), env_prefix="SPARK"
        )
        self.secrets = MemorySecrets()


@pytest.fixture
def server_app(tmp_path: Path, server_db: DatabaseSettings) -> Iterator[Any]:  # noqa: F811
    config = tmp_path / "config.yaml"
    config.write_text(f"server:\n  public_base_url: {PUBLIC}\n", encoding="utf-8", newline="\n")

    def start() -> Any:
        mode.configure(RuntimeSettings(Mode.SERVER, server_db))
        app = create_app(_EnvCtx(config))  # type: ignore[arg-type]
        attach_database(app, server_db, True)
        attach_identity(app)
        return app

    try:
        yield start
    finally:
        mode.reset()


@pytest.mark.parametrize("value", ["false", "true", "on", "1"])
def test_the_heartbeat_stays_off_on_a_server(
    server_app: Any, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED", value)
    app = server_app()
    with TestClient(app, base_url=PUBLIC, backend_options=client_options()) as client:
        settings = client.app.state.ctx.settings  # type: ignore[attr-defined]
        assert settings.get("interface.browser_heartbeat.enabled", True) == value
        assert _heartbeat_config(client.app.state.ctx) is None  # type: ignore[attr-defined]
        assert client.get("/loading/api/status").status_code == 200
