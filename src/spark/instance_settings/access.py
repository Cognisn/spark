"""Who may change what: the administrator check, a stub until wave 3.

Endpoints ask :func:`access_check` whether the request comes from an
administrator and which user it is. Until warden arrives (wave 3) the only
identity SPARK has is the operator who signed in with the start-up code, so the
stub :class:`SingleOperatorAccess` treats that operator as the administrator and
as the one user (``app.state.user_guid``). Wave 3 calls :func:`set_access_check`
with a check backed by warden permissions.
"""

from __future__ import annotations

from typing import Protocol

from starlette.requests import Request

__all__ = ["AccessCheck", "SingleOperatorAccess", "access_check", "reset", "set_access_check"]


class AccessCheck(Protocol):
    def is_administrator(self, request: Request) -> bool: ...

    def user_guid(self, request: Request) -> str: ...


class SingleOperatorAccess:
    """The pre-warden stub: the signed-in operator is the administrator and the only user."""

    def is_administrator(self, request: Request) -> bool:
        return True

    def user_guid(self, request: Request) -> str:
        return str(getattr(request.app.state, "user_guid", "") or "")


_DEFAULT = SingleOperatorAccess()
_check: AccessCheck = _DEFAULT


def access_check() -> AccessCheck:
    return _check


def set_access_check(check: AccessCheck) -> None:
    """Replace the administrator check (wave 3: warden permissions)."""
    global _check
    _check = check


def reset() -> None:
    """Restore the stub (tests)."""
    global _check
    _check = _DEFAULT
