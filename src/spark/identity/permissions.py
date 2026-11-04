"""SPARK's permissions and roles, declared to warden (A5).

Both are data. :data:`PERMISSIONS` names every capability a role can grant:
one ``spark.tools.<category>`` permission per built-in tool category, plus the
capabilities that are not tools (scheduled actions, sub-agents, instance-wide
knowledge graphs, instance settings, user administration, the audit trail and
a user's own API keys). :data:`ROLES` maps each role to the permissions it
holds. A new role is a new entry in :data:`ROLES`, and a new capability a new
entry in :data:`PERMISSIONS`; :func:`build_registry` declares whatever they
hold, so nothing else changes.

warden stores only which users hold which roles; the roles themselves are
defined here, in code, at start-up.

Holding a tool permission does not override the deployment mode: a server
refuses the local-only tool categories (A6) whatever the role, so only the
administrator holds their permissions, and only because it holds everything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from warden.identity import PermissionRegistry

from spark.tools.registry import LOCAL_ONLY_CATEGORIES, TOOL_CATEGORIES, permission_name

__all__ = [
    "ADMINISTRATOR",
    "AUDITOR",
    "PERMISSIONS",
    "POWER_USER",
    "ROLES",
    "USER",
    "RoleSpec",
    "build_registry",
    "role_permissions",
]

ACTIONS = "spark.actions"
AGENTS = "spark.agents"
KNOWLEDGE_INSTANCE = "spark.knowledge.instance"
SETTINGS_INSTANCE = "spark.settings.instance"
USERS_ADMIN = "spark.users.admin"
AUDIT_READ = "spark.audit.read"
AUDIT_EXPORT = "spark.audit.export"
API_KEYS = "spark.api_keys"
USAGE_ADMIN = "spark.usage.admin"

_TOOL_PERMISSIONS: dict[str, str] = {
    permission_name(category): f"Use the {category.replace('_', ' ')} tools"
    for category in TOOL_CATEGORIES
}

# Permission name -> what it allows.
PERMISSIONS: Mapping[str, str] = {
    **_TOOL_PERMISSIONS,
    ACTIONS: "Create and run scheduled actions",
    AGENTS: "Spawn sub-agents",
    KNOWLEDGE_INSTANCE: "Build and change instance-wide knowledge graphs",
    SETTINGS_INSTANCE: "Change instance settings and secrets",
    USERS_ADMIN: "Administer users, roles, invitations and API keys",
    AUDIT_READ: "Read the audit trail",
    AUDIT_EXPORT: "Export the audit trail",
    API_KEYS: "Manage one's own API keys",
    USAGE_ADMIN: "View and export every user's usage and set token budgets",
}


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """A role: the permissions it holds, and whether it is an administrator role.

    warden protects the last active holder of an administrator role.
    """

    permissions: frozenset[str]
    administrator: bool = False


ADMINISTRATOR = "administrator"
AUDITOR = "auditor"
POWER_USER = "power_user"
USER = "user"

_USER_PERMISSIONS: frozenset[str] = frozenset(
    {
        permission_name(category)
        for category in TOOL_CATEGORIES
        if category not in LOCAL_ONLY_CATEGORIES and category != "agents"
    }
    | {API_KEYS}
)

# Role name -> its permissions.
ROLES: Mapping[str, RoleSpec] = {
    ADMINISTRATOR: RoleSpec(frozenset(PERMISSIONS), administrator=True),
    AUDITOR: RoleSpec(frozenset({AUDIT_READ, AUDIT_EXPORT})),
    POWER_USER: RoleSpec(
        _USER_PERMISSIONS | {ACTIONS, AGENTS, KNOWLEDGE_INSTANCE, permission_name("agents")}
    ),
    USER: RoleSpec(_USER_PERMISSIONS),
}


def role_permissions(role: str) -> frozenset[str]:
    """The permissions ``role`` holds in the catalogue (none for a role it does not define)."""
    spec = ROLES.get(role)
    return spec.permissions if spec is not None else frozenset()


def build_registry() -> PermissionRegistry:
    """A warden registry holding every permission and role in the catalogue."""
    registry = PermissionRegistry()
    for name, description in PERMISSIONS.items():
        registry.register_permission(name, description)
    for role, spec in ROLES.items():
        registry.define_role(role, spec.permissions, administrator=spec.administrator)
    return registry
