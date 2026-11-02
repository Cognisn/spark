"""The request's identity and its permissions (A5, D32).

Server mode: the signed-in user is ``request.state.identity`` and every check
fails closed without one. Personal mode: the local operator holds everything.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from warden.identity import Principal

from spark import mode
from spark.identity import access
from spark.identity.access import (
    SparkIdentity,
    WardenAccess,
    current_identity,
    has_permission,
    identity_from_principal,
    request_permissions,
    require_permission,
)
from spark.identity.permissions import (
    ADMINISTRATOR,
    AUDITOR,
    PERMISSIONS,
    POWER_USER,
    USER,
    role_permissions,
)
from spark.instance_settings import access as settings_access
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from tests.identity_helpers import AsIdentity, Holder, identity_for

SETTINGS = "spark.settings.instance"


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(
        RuntimeSettings(
            Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://db.example.com/x")
        )
    )
    yield
    mode.reset()


@pytest.fixture
def personal_mode(tmp_path: Path) -> Iterator[None]:
    mode.configure(
        RuntimeSettings(
            Mode.PERSONAL, DatabaseSettings(Dialect.SQLITE, sqlite_path=tmp_path / "s.db")
        )
    )
    yield
    mode.reset()


@pytest.fixture
def holder() -> Holder:
    return Holder()


@pytest.fixture
def client(holder: Holder) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.user_guid = "local-operator"

    @app.get("/who")
    def who(request: Request) -> dict[str, object]:
        identity = current_identity(request)
        permissions = request_permissions(request)
        return {
            "guid": identity.user_guid if identity else None,
            "permissions": sorted(permissions) if permissions is not None else None,
            "settings": has_permission(request, SETTINGS),
            "admin": WardenAccess().is_administrator(request),
            "access_guid": WardenAccess().user_guid(request),
        }

    @app.get("/settings-only")
    def settings_only(
        identity: Annotated[SparkIdentity, Depends(require_permission(SETTINGS))],
    ) -> dict[str, str]:
        return {"guid": identity.user_guid}

    app.add_middleware(AsIdentity, holder=holder)
    with TestClient(app) as test_client:
        yield test_client


class TestIdentityFromPrincipal:
    def test_copies_warden_principal(self) -> None:
        user_id = uuid.uuid4()
        principal = Principal(
            user_id=user_id,
            session_id=uuid.uuid4(),
            email="ada@example.com",
            roles=frozenset({USER}),
            permissions=role_permissions(USER),
            amr=("pwd",),
        )
        identity = identity_from_principal(principal, "guid-1")
        assert identity == SparkIdentity(
            user_guid="guid-1",
            warden_user_id=str(user_id),
            email="ada@example.com",
            roles=frozenset({USER}),
            permissions=role_permissions(USER),
        )


class TestServerMode:
    def test_no_identity_holds_nothing(self, server_mode: None, client: TestClient) -> None:
        body = client.get("/who").json()
        assert body == {
            "guid": None,
            "permissions": None,
            "settings": False,
            "admin": False,
            "access_guid": "",
        }

    def test_identity_on_request_state(
        self, server_mode: None, client: TestClient, holder: Holder
    ) -> None:
        holder.identity = identity_for(USER, user_guid="guid-user")
        body = client.get("/who").json()
        assert body["guid"] == "guid-user"
        assert body["access_guid"] == "guid-user"
        assert body["permissions"] == sorted(role_permissions(USER))

    @pytest.mark.parametrize(
        ("role", "allowed"),
        [(ADMINISTRATOR, True), (AUDITOR, False), (POWER_USER, False), (USER, False)],
    )
    def test_has_permission_and_administrator_by_role(
        self, server_mode: None, client: TestClient, holder: Holder, role: str, allowed: bool
    ) -> None:
        holder.identity = identity_for(role)
        body = client.get("/who").json()
        assert body["settings"] is allowed
        assert body["admin"] is allowed

    def test_require_permission_without_identity_is_401(
        self, server_mode: None, client: TestClient
    ) -> None:
        assert client.get("/settings-only").status_code == 401

    @pytest.mark.parametrize("role", [AUDITOR, POWER_USER, USER])
    def test_require_permission_without_the_permission_is_403(
        self, server_mode: None, client: TestClient, holder: Holder, role: str
    ) -> None:
        holder.identity = identity_for(role)
        assert client.get("/settings-only").status_code == 403

    def test_require_permission_with_the_permission(
        self, server_mode: None, client: TestClient, holder: Holder
    ) -> None:
        holder.identity = identity_for(ADMINISTRATOR, user_guid="guid-admin")
        resp = client.get("/settings-only")
        assert resp.status_code == 200
        assert resp.json() == {"guid": "guid-admin"}

    def test_something_else_on_request_state_is_not_an_identity(
        self, server_mode: None, client: TestClient, holder: Holder
    ) -> None:
        holder.identity = {"permissions": list(PERMISSIONS)}  # type: ignore[assignment]
        assert client.get("/who").json()["permissions"] is None
        assert client.get("/settings-only").status_code == 401


class TestPersonalMode:
    def test_local_operator_holds_everything(self, personal_mode: None, client: TestClient) -> None:
        body = client.get("/who").json()
        assert body["guid"] == "local-operator"
        assert body["access_guid"] == "local-operator"
        assert body["permissions"] == sorted(PERMISSIONS)
        assert body["settings"] is True
        assert body["admin"] is True
        assert client.get("/settings-only").status_code == 200

    def test_unconfigured_mode_is_personal(self, client: TestClient) -> None:
        mode.reset()
        assert client.get("/who").json()["admin"] is True


class TestInstall:
    def test_server_installs_warden_access(self, server_mode: None) -> None:
        access.install()
        assert isinstance(settings_access.access_check(), WardenAccess)

    def test_personal_keeps_the_single_operator(self, personal_mode: None) -> None:
        access.install()
        assert isinstance(settings_access.access_check(), settings_access.SingleOperatorAccess)

    def test_server_app_never_runs_on_the_stub(
        self, server_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from spark.web.server import create_app

        monkeypatch.setattr("spark.core.application._get_config_path", lambda: tmp_path / "c.yaml")
        ctx = MagicMock()
        ctx.settings.get = lambda key, default=None, **_: default
        create_app(ctx, first_run=False)
        assert isinstance(settings_access.access_check(), WardenAccess)

    def test_personal_app_keeps_the_stub(
        self, personal_mode: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        from spark.web.server import create_app

        monkeypatch.setattr("spark.core.application._get_config_path", lambda: tmp_path / "c.yaml")
        ctx = MagicMock()
        ctx.settings.get = lambda key, default=None, **_: default
        create_app(ctx, first_run=False)
        assert isinstance(settings_access.access_check(), settings_access.SingleOperatorAccess)
