"""SPARK's navigation (server mode): the Account menu and the Administration area.

Two template globals:

* ``identity_menu`` lists the signed-in user's own pages, for the Account menu
  ``base.html`` renders with sign-out;
* ``admin_menu`` lists the Administration area's groups, each entry shown only
  to a user holding the permission its page already requires. A user holding
  none of them has no Administration area: no link, and ``/admin`` refuses.

Both are empty in personal mode (no warden attached), whose one local user has
the Settings page instead. Navigation decides nothing about access: every page
keeps its own guard.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.requests import Request

from spark.identity.access import current_identity
from spark.identity.permissions import (
    ADMIN_CONNECTIONS,
    API_KEYS,
    AUDIT_READ,
    BUNDLES_IMPORT,
    SETTINGS_INSTANCE,
    USAGE_ADMIN,
    USERS_ADMIN,
)

__all__ = [
    "ADMIN_GROUPS",
    "ADMIN_PATH",
    "ADMIN_PERMISSIONS",
    "MenuEntry",
    "MenuGroup",
    "admin_menu",
    "identity_menu",
]

ADMIN_PATH = "/admin"


@dataclass(frozen=True, slots=True)
class MenuEntry:
    href: str
    icon: str
    label: str
    # The permission the entry needs; None for every signed-in user.
    permission: str | None = None
    # One sentence on what the page is for (the Administration landing page).
    description: str = ""


@dataclass(frozen=True, slots=True)
class MenuGroup:
    label: str
    entries: tuple[MenuEntry, ...]


_ACCOUNT = (
    MenuEntry("/account", "bi-person", "My account"),
    MenuEntry("/account/api-keys", "bi-key", "My API keys", API_KEYS),
    MenuEntry("/account/preferences", "bi-sliders", "My preferences"),
)

ADMIN_GROUPS: tuple[MenuGroup, ...] = (
    MenuGroup(
        "Users and access",
        (
            MenuEntry(
                "/admin/users",
                "bi-people",
                "Users",
                USERS_ADMIN,
                "Who can sign in, their roles and status.",
            ),
            MenuEntry(
                "/admin/invitations",
                "bi-envelope-plus",
                "Invitations",
                USERS_ADMIN,
                "Invite people and follow pending invitations.",
            ),
            MenuEntry(
                "/admin/sso",
                "bi-building-lock",
                "Single sign-on",
                USERS_ADMIN,
                "Sign-in through your organisation's identity provider.",
            ),
            MenuEntry(
                "/admin/api-keys",
                "bi-key-fill",
                "API keys",
                USERS_ADMIN,
                "Every user's API keys, and revoking them.",
            ),
        ),
    ),
    # The other areas, each its own entry (label "" renders no group heading).
    MenuGroup(
        "",
        (
            MenuEntry(
                "/admin/connections",
                "bi-plug",
                "Connections and models",
                ADMIN_CONNECTIONS,
                "The model services this Spark uses, their models, metering and assignments.",
            ),
            MenuEntry(
                "/admin/tools",
                "bi-tools",
                "Tools and MCP servers",
                SETTINGS_INSTANCE,
                "Built-in tools, tool results and the MCP servers offered to users.",
            ),
            MenuEntry(
                "/admin/mail",
                "bi-envelope",
                "Mail",
                SETTINGS_INSTANCE,
                "The mail server for invitations and password resets.",
            ),
            MenuEntry(
                "/admin/security",
                "bi-shield-lock",
                "Security",
                SETTINGS_INSTANCE,
                "Prompt inspection, tool permissions and tool result inspection.",
            ),
            MenuEntry(
                "/admin/settings",
                "bi-sliders",
                "Instance settings",
                SETTINGS_INSTANCE,
                "Defaults every user shares: model, conversation, agents, actions and voice.",
            ),
            MenuEntry(
                "/admin/usage",
                "bi-bar-chart",
                "Usage and budgets",
                USAGE_ADMIN,
                "Every user's usage, and their token budgets.",
            ),
            MenuEntry(
                "/admin/audit",
                "bi-journal-text",
                "Audit trail",
                AUDIT_READ,
                "Who did what, and when.",
            ),
            MenuEntry(
                "/admin/bundles",
                "bi-box-arrow-in-down",
                "User bundles",
                BUNDLES_IMPORT,
                "Import a user's data exported from a personal Spark.",
            ),
        ),
    ),
)

#: Holding any one of these opens the Administration area.
ADMIN_PERMISSIONS: frozenset[str] = frozenset(
    entry.permission
    for group in ADMIN_GROUPS
    for entry in group.entries
    if entry.permission is not None
)


def _permissions(request: Request) -> frozenset[str] | None:
    """The signed-in user's permissions, or None in personal mode or signed out."""
    if not getattr(request.app.state, "identity_attached", False):
        return None
    identity = current_identity(request)
    if identity is None:
        return None
    return frozenset(identity.permissions)


def identity_menu(request: Request) -> list[MenuEntry]:
    """The Account menu: the user's own pages their permissions allow; none in personal mode."""
    held = _permissions(request)
    if held is None:
        return []
    return [entry for entry in _ACCOUNT if entry.permission is None or entry.permission in held]


def admin_menu(request: Request) -> list[MenuGroup]:
    """The Administration groups the user may open, each with only its allowed entries.

    Empty in personal mode, signed out, or for a user holding no administrative
    permission.
    """
    held = _permissions(request)
    if held is None:
        return []
    groups = []
    for group in ADMIN_GROUPS:
        entries = tuple(entry for entry in group.entries if entry.permission in held)
        if entries:
            groups.append(MenuGroup(group.label, entries))
    return groups
