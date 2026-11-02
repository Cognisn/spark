"""The request's identity and what it may do (A5).

In server mode the signed-in user is a :class:`SparkIdentity` on
``request.state.identity``: the sign-in layer builds it from warden's
``Principal`` and the user's SPARK guid (:func:`identity_from_principal`) and
stores it there for the rest of the request. Every check fails closed: a
request without an identity holds no permission.

Personal mode has one local operator, who holds every permission, as before.

:class:`WardenAccess` answers :mod:`spark.instance_settings.access`'s
administrator question from these permissions; :func:`install` puts it in place
of the single-operator stub when a server starts (D32), so a server never
serves more than one user on the stub.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from warden.identity import Principal

from spark.identity.permissions import ADMINISTRATOR, PERMISSIONS, SETTINGS_INSTANCE
from spark.instance_settings import access as settings_access
from spark.tools.registry import PERMISSIONS_KEY

__all__ = [
    "STATE_KEY",
    "SparkIdentity",
    "WardenAccess",
    "current_identity",
    "has_permission",
    "identity_from_principal",
    "install",
    "request_permissions",
    "require_permission",
    "tools_config_for",
]

# The attribute of ``request.state`` that holds the signed-in user (server mode).
STATE_KEY = "identity"


@dataclass(frozen=True, slots=True)
class SparkIdentity:
    """The user behind a request: their SPARK guid, warden user, roles and permissions."""

    user_guid: str
    warden_user_id: str
    email: str
    roles: frozenset[str]
    permissions: frozenset[str]


def identity_from_principal(principal: Principal, user_guid: str) -> SparkIdentity:
    """The identity of a warden ``Principal`` whose SPARK guid is ``user_guid``.

    ``user_guid`` comes from :func:`spark.database.user_identities.user_guid_for`
    with ``str(principal.user_id)``. The permissions are warden's (for an API
    key, already narrowed to the key's scopes).
    """
    return SparkIdentity(
        user_guid=user_guid,
        warden_user_id=str(principal.user_id),
        email=principal.email,
        roles=principal.roles,
        permissions=principal.permissions,
    )


def _server() -> bool:
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        # A process that never configured its mode (a test, a script) is personal.
        return False


def _local_operator(request: Request) -> SparkIdentity:
    guid = str(getattr(request.app.state, "user_guid", "") or "")
    return SparkIdentity(
        user_guid=guid,
        warden_user_id="",
        email="",
        roles=frozenset({ADMINISTRATOR}),
        permissions=frozenset(PERMISSIONS),
    )


def current_identity(request: Request) -> SparkIdentity | None:
    """The signed-in user, or None (server mode only; personal mode has its local operator)."""
    if not _server():
        return _local_operator(request)
    identity = getattr(request.state, STATE_KEY, None)
    return identity if isinstance(identity, SparkIdentity) else None


def request_permissions(request: Request) -> frozenset[str] | None:
    """The permissions the request's user holds, None when no one is signed in.

    This is what a request hands the tools configuration (``_permissions``).
    """
    identity = current_identity(request)
    return identity.permissions if identity is not None else None


def tools_config_for(request: Request, config: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of an embedded tools configuration carrying the request's user's permissions.

    Built-in tools offered or run with it follow that user's roles (server mode).
    """
    return {**config, PERMISSIONS_KEY: request_permissions(request)}


def has_permission(request: Request, name: str) -> bool:
    """Whether the request's user holds permission ``name`` (never without a signed-in user)."""
    permissions = request_permissions(request)
    return permissions is not None and name in permissions


def require_permission(name: str) -> Callable[[Request], SparkIdentity]:
    """A FastAPI dependency: 401 without a signed-in user, 403 without ``name``."""

    def dependency(request: Request) -> SparkIdentity:
        identity = current_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="not signed in")
        if name not in identity.permissions:
            raise HTTPException(status_code=403, detail="forbidden")
        return identity

    return dependency


class WardenAccess:
    """The instance-settings check backed by warden permissions.

    The administrator of instance settings is whoever holds
    ``spark.settings.instance``; the user is the signed-in identity's guid.
    """

    def is_administrator(self, request: Request) -> bool:
        return has_permission(request, SETTINGS_INSTANCE)

    def user_guid(self, request: Request) -> str:
        identity = current_identity(request)
        return identity.user_guid if identity is not None else ""


def install() -> None:
    """In server mode, replace the single-operator stub with :class:`WardenAccess` (D32)."""
    if _server():
        settings_access.set_access_check(WardenAccess())
