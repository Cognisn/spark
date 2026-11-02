"""Test-only helpers: sign a request in as a given SPARK identity.

Production sets ``request.state.identity`` in its sign-in layer; tests put it
there directly with :class:`AsIdentity`, a pure ASGI middleware reading a
mutable holder, so one client can act as different users.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from spark.identity.access import STATE_KEY, SparkIdentity
from spark.identity.permissions import role_permissions


def identity_for(*roles: str, user_guid: str | None = None) -> SparkIdentity:
    """An identity holding ``roles`` with their catalogue permissions."""
    warden_user = str(uuid.uuid4())
    permissions: frozenset[str] = frozenset()
    for role in roles:
        permissions |= role_permissions(role)
    return SparkIdentity(
        user_guid=user_guid or warden_user,
        warden_user_id=warden_user,
        email=f"{'-'.join(roles) or 'nobody'}@example.com",
        roles=frozenset(roles),
        permissions=permissions,
    )


@dataclass
class Holder:
    """The identity requests carry; None means not signed in."""

    identity: SparkIdentity | None = None


class AsIdentity:
    """Set ``request.state.identity`` from a :class:`Holder` on every HTTP request."""

    def __init__(self, app: ASGIApp, holder: Holder) -> None:
        self.app = app
        self.holder = holder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.holder.identity is not None:
            state: dict[str, Any] = scope.setdefault("state", {})
            state[STATE_KEY] = self.holder.identity
        await self.app(scope, receive, send)
