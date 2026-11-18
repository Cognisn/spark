"""Who is an administrator of connections (M9): the holder of spark.admin.connections.

A connection with no assignment is visible to administrators only. Both places that
decide it, ``spark.web.models.user_scope`` (the model lists) and
``ConversationManager._user_scope`` (a turn), ask for ``spark.admin.connections``;
``spark.settings.instance`` alone no longer makes one. Personal mode has no scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from spark import mode
from spark.core.conversation_manager import ConversationManager
from spark.identity import access
from spark.identity.access import SparkIdentity
from spark.identity.permissions import (
    ADMIN_CONNECTIONS,
    ADMINISTRATOR,
    PERMISSIONS,
    ROLES,
    SETTINGS_INSTANCE,
    USER,
)
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings
from spark.tools.registry import ANY_ROLE
from spark.web import models


@pytest.fixture()
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    try:
        yield
    finally:
        mode.reset()


def test_the_permission_is_declared_and_only_administrators_hold_it() -> None:
    assert ADMIN_CONNECTIONS == "spark.admin.connections"
    assert ADMIN_CONNECTIONS in PERMISSIONS
    holders = {role for role, spec in ROLES.items() if ADMIN_CONNECTIONS in spec.permissions}
    assert holders == {ADMINISTRATOR}


class TestWebUserScope:
    def _scope(self, monkeypatch: pytest.MonkeyPatch, permissions: frozenset[str]) -> Any:
        identity = SparkIdentity(
            user_guid="guid-ada",
            warden_user_id="",
            email="ada@example.com",
            roles=frozenset({USER}),
            permissions=permissions,
        )
        monkeypatch.setattr(access, "current_identity", lambda request: identity)
        monkeypatch.setattr(access, "current_user_guid", lambda request: identity.user_guid)
        monkeypatch.setattr(
            access, "has_permission", lambda request, name: name in identity.permissions
        )
        return models.user_scope(object())  # type: ignore[arg-type]

    def test_the_connections_permission_makes_an_administrator(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scope = self._scope(monkeypatch, frozenset({ADMIN_CONNECTIONS}))
        assert scope.administrator is True
        assert scope.roles == frozenset({USER})

    def test_instance_settings_alone_do_not(
        self, server_mode: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._scope(monkeypatch, frozenset({SETTINGS_INSTANCE})).administrator is False

    def test_personal_mode_has_no_scope(self) -> None:
        mode.reset()
        assert models.user_scope(object()) is None  # type: ignore[arg-type]


class TestTurnUserScope:
    def test_the_connections_permission_makes_an_administrator(self) -> None:
        scope = ConversationManager._user_scope("g", ["user"], frozenset({ADMIN_CONNECTIONS}))
        assert scope.administrator is True

    def test_instance_settings_alone_do_not(self) -> None:
        scope = ConversationManager._user_scope("g", [], frozenset({SETTINGS_INSTANCE}))
        assert scope.administrator is False

    def test_the_deployment_wide_marker_is_never_a_user(self) -> None:
        assert ConversationManager._user_scope("g", [], ANY_ROLE).administrator is False
        assert ConversationManager._user_scope("g", [], None).administrator is False
