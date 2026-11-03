"""Server mode: a request's user holds ``spark.mcp.<server>`` for the servers granted to their roles.

The grants live in the ``mcp.servers`` instance setting (A12). Each signed-in
request's identity gains the permissions of the servers listed for one of its
roles; a request authenticated by an API key gains none (its scopes are fixed
when the key is made). Personal mode is untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from spark import mode
from spark.identity.access import STATE_KEY, SparkIdentity
from spark.instance_settings import registry
from spark.mcp_integration import access
from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

SERVERS = [
    {"name": "search", "transport": "http", "url": "https://s.example.com/mcp", "roles": ["user"]},
    {
        "name": "finance",
        "transport": "sse",
        "url": "https://f.example.com/sse",
        "roles": ["power_user"],
    },
]


class Source:
    def __init__(self, servers: list[dict[str, Any]]) -> None:
        self.servers = servers

    def get(self, key: str) -> Any:
        return self.servers if key == "mcp.servers" else registry.spec(key).default


def _identity(*roles: str) -> SparkIdentity:
    return SparkIdentity(
        user_guid="u-1",
        warden_user_id="w-1",
        email="ada@example.com",
        roles=frozenset(roles),
        permissions=frozenset({"spark.tools.web"}),
    )


def _request(identity: SparkIdentity | None, *, api_key: bool = False) -> Any:
    state = SimpleNamespace()
    if identity is not None:
        setattr(state, STATE_KEY, identity)
    if api_key:
        state.warden_api_key = True
    app = SimpleNamespace(state=SimpleNamespace(instance_settings=Source(SERVERS)))
    return SimpleNamespace(state=state, app=app)


@pytest.fixture
def server_mode() -> Iterator[None]:
    mode.configure(RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="x")))
    yield
    mode.reset()


class TestServerMode:
    def test_identity_gains_the_grants_of_its_roles(self, server_mode: None) -> None:
        request = _request(_identity("user"))
        access.apply_grants(request)
        held = getattr(request.state, STATE_KEY).permissions
        assert held == {"spark.tools.web", "spark.mcp.search"}

    def test_every_role_counts(self, server_mode: None) -> None:
        request = _request(_identity("user", "power_user"))
        access.apply_grants(request)
        assert {"spark.mcp.search", "spark.mcp.finance"} <= getattr(
            request.state, STATE_KEY
        ).permissions

    def test_a_role_with_no_grant_gains_nothing(self, server_mode: None) -> None:
        request = _request(_identity("auditor"))
        access.apply_grants(request)
        assert getattr(request.state, STATE_KEY).permissions == {"spark.tools.web"}

    def test_api_key_requests_gain_nothing(self, server_mode: None) -> None:
        request = _request(_identity("user"), api_key=True)
        access.apply_grants(request)
        assert getattr(request.state, STATE_KEY).permissions == {"spark.tools.web"}

    def test_signed_out_request_is_left_alone(self, server_mode: None) -> None:
        request = _request(None)
        access.apply_grants(request)
        assert not hasattr(request.state, STATE_KEY)

    def test_everything_else_about_the_identity_is_kept(self, server_mode: None) -> None:
        before = _identity("user")
        request = _request(before)
        access.apply_grants(request)
        after = getattr(request.state, STATE_KEY)
        assert (after.user_guid, after.warden_user_id, after.email, after.roles) == (
            before.user_guid,
            before.warden_user_id,
            before.email,
            before.roles,
        )

    def test_without_instance_settings_nothing_is_granted(self, server_mode: None) -> None:
        request = _request(_identity("user"))
        request.app.state.instance_settings = None
        access.apply_grants(request)
        assert getattr(request.state, STATE_KEY).permissions == {"spark.tools.web"}


class TestPersonalMode:
    def test_personal_mode_is_untouched(self) -> None:
        identity = _identity("user")
        request = _request(identity)
        access.apply_grants(request)
        assert getattr(request.state, STATE_KEY) is identity
