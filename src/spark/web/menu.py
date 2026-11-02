"""The identity screens' menu entries (server mode), each shown only to a role that may use it.

``identity_menu`` is a template global: ``base.html`` renders an Account menu
from it. In personal mode (no warden attached) it is empty, so personal mode's
menu is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import Request

from spark.identity.access import current_identity
from spark.identity.permissions import API_KEYS, AUDIT_READ, USERS_ADMIN

__all__ = ["MenuEntry", "identity_menu"]


@dataclass(frozen=True, slots=True)
class MenuEntry:
    href: str
    icon: str
    label: str
    # The permission the entry needs; None for every signed-in user.
    permission: str | None = None


_ENTRIES = (
    MenuEntry("/account", "bi-person", "My account"),
    MenuEntry("/account/api-keys", "bi-key", "My API keys", API_KEYS),
    MenuEntry("/admin/users", "bi-people", "Users", USERS_ADMIN),
    MenuEntry("/admin/invitations", "bi-envelope-plus", "Invitations", USERS_ADMIN),
    MenuEntry("/admin/api-keys", "bi-key-fill", "All API keys", USERS_ADMIN),
    MenuEntry("/audit", "bi-journal-text", "Audit trail", AUDIT_READ),
)


def identity_menu(request: Request) -> list[MenuEntry]:
    """The entries the signed-in user's permissions allow; none in personal mode."""
    if not getattr(request.app.state, "identity_attached", False):
        return []
    identity = current_identity(request)
    if identity is None:
        return []
    return [
        entry
        for entry in _ENTRIES
        if entry.permission is None or entry.permission in identity.permissions
    ]
