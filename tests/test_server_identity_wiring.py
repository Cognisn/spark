"""``create_and_serve`` signs a server in through warden and pins its public origin.

Personal mode keeps its login code and its bind-address Origin policy (D18,
Q14); server mode never has either: warden is the authenticator and the Origin
policy is exactly ``server.public_base_url``, whatever address it binds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from spark import mode
from spark.identity.web import WardenAuthenticator
from spark.instance_settings.startup import StartupConfigError
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.web import server
from spark.web.auth import PersonalAuthenticator
from spark.web.security import OriginPolicy


class Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, *, cast: Any = None) -> Any:
        value = self.values.get(key, default)
        return cast(value) if cast is not None and value is not None else value


class Ctx:
    def __init__(self, values: dict[str, Any]) -> None:
        self.settings = Settings(values)
        self.secrets = None


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[FastAPI]]:
    apps: list[FastAPI] = []
    real_create = server.create_app

    def create(ctx: Any, *, first_run: bool = False) -> FastAPI:
        app = real_create(ctx, first_run=first_run)
        apps.append(app)
        return app

    async def serve(self: Any) -> None:
        return None

    opened: list[str] = []
    monkeypatch.setattr(server, "create_app", create)
    monkeypatch.setattr(server, "_background_init", lambda app, ctx: None)
    monkeypatch.setattr(server.uvicorn.Server, "serve", serve)
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    monkeypatch.setattr("spark.core.application._get_data_path", lambda: tmp_path)
    yield apps
    mode.reset()


def _values(**extra: Any) -> dict[str, Any]:
    return {"interface.host": "0.0.0.0", "interface.port": 22411, **extra}


def test_server_signs_in_through_warden_and_pins_the_public_origin(
    served: list[FastAPI],
) -> None:
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y"))
    )
    ctx = Ctx(_values(**{"server.public_base_url": "https://spark.example.com"}))
    asyncio.run(server.create_and_serve(ctx))  # type: ignore[arg-type]
    app = served[0]
    assert isinstance(app.state.authenticator, WardenAuthenticator)
    assert app.state.origin_policy == OriginPolicy(frozenset({"https://spark.example.com"}))
    assert not app.state.origin_policy.match_host
    assert app.state.identity_attached is True
    assert app.state.database_auto_migrate is False


def test_server_never_asks_for_the_shared_secrets_user_guid(
    served: list[FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server's users come from warden; the process has no user of its own."""
    import spark.core.user_guid as user_guid

    def _refuse(ctx: object | None = None) -> str:
        raise AssertionError("get_user_guid called in server mode")

    monkeypatch.setattr(user_guid, "get_user_guid", _refuse)
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y"))
    )
    ctx = Ctx(_values(**{"server.public_base_url": "https://spark.example.com"}))
    asyncio.run(server.create_and_serve(ctx))  # type: ignore[arg-type]
    assert served[0].state.user_guid is None


def test_personal_keeps_its_single_local_user(
    served: list[FastAPI], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import spark.core.user_guid as user_guid

    monkeypatch.setattr(user_guid, "get_user_guid", lambda ctx=None: "local-guid")
    mode.configure(
        RuntimeSettings(Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s"))
    )
    asyncio.run(server.create_and_serve(Ctx(_values(**{"interface.open_browser": False}))))  # type: ignore[arg-type]
    assert served[0].state.user_guid == "local-guid"


def test_server_without_a_public_address_refuses_to_start(served: list[FastAPI]) -> None:
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x/y"))
    )
    with pytest.raises(StartupConfigError, match="server.public_base_url"):
        asyncio.run(server.create_and_serve(Ctx(_values())))  # type: ignore[arg-type]


def test_personal_keeps_its_login_and_bind_address_policy(
    served: list[FastAPI], tmp_path: Path
) -> None:
    mode.configure(
        RuntimeSettings(Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s"))
    )
    ctx = Ctx(_values(**{"interface.open_browser": False}))
    asyncio.run(server.create_and_serve(ctx))  # type: ignore[arg-type]
    app = served[0]
    assert isinstance(app.state.authenticator, PersonalAuthenticator)
    assert app.state.origin_policy == OriginPolicy.same_host([])
    assert not getattr(app.state, "identity_attached", False)
