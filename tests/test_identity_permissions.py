"""SPARK's permissions and roles for warden (A5).

The catalogue is data: every tool category has a ``spark.tools.<category>``
permission, the four roles are permission sets, and a new role is a new entry
in ``ROLES`` with no code change elsewhere.
"""

from __future__ import annotations

import pytest
from warden.identity import PermissionRegistry

from spark.identity import permissions
from spark.identity.permissions import (
    ADMINISTRATOR,
    AUDITOR,
    PERMISSIONS,
    POWER_USER,
    ROLES,
    USER,
    RoleSpec,
    build_registry,
    role_permissions,
)
from spark.tools.registry import LOCAL_ONLY_CATEGORIES, TOOL_CATEGORIES, permission_name


class TestCatalogue:
    def test_every_tool_category_has_a_permission(self) -> None:
        for category in TOOL_CATEGORIES:
            assert permission_name(category) in PERMISSIONS, category

    @pytest.mark.parametrize(
        "name",
        [
            "spark.actions",
            "spark.agents",
            "spark.knowledge.instance",
            "spark.settings.instance",
            "spark.users.admin",
            "spark.audit.read",
            "spark.audit.export",
            "spark.api_keys",
        ],
    )
    def test_capability_permissions(self, name: str) -> None:
        assert PERMISSIONS[name]

    def test_every_permission_has_a_description(self) -> None:
        assert all(description.strip() for description in PERMISSIONS.values())

    def test_the_four_roles(self) -> None:
        assert set(ROLES) == {ADMINISTRATOR, AUDITOR, POWER_USER, USER}

    def test_roles_use_only_declared_permissions(self) -> None:
        for name, spec in ROLES.items():
            assert spec.permissions <= PERMISSIONS.keys(), name


class TestRoles:
    def test_administrator_holds_everything(self) -> None:
        assert role_permissions(ADMINISTRATOR) == frozenset(PERMISSIONS)
        assert ROLES[ADMINISTRATOR].administrator is True

    def test_only_the_administrator_is_an_administrator_role(self) -> None:
        assert [n for n, spec in ROLES.items() if spec.administrator] == [ADMINISTRATOR]

    def test_auditor_holds_exactly_audit_read_and_export(self) -> None:
        assert role_permissions(AUDITOR) == {"spark.audit.read", "spark.audit.export"}

    def test_user(self) -> None:
        expected = {
            permission_name(c)
            for c in TOOL_CATEGORIES
            if c not in LOCAL_ONLY_CATEGORIES and c != "agents"
        } | {"spark.api_keys"}
        assert role_permissions(USER) == expected

    def test_power_user_is_user_plus_actions_agents_and_instance_graphs(self) -> None:
        assert role_permissions(POWER_USER) == role_permissions(USER) | {
            "spark.actions",
            "spark.agents",
            "spark.tools.agents",
            "spark.knowledge.instance",
        }

    def test_power_user_cannot_spawn_agents_without_the_agents_tools(self) -> None:
        # spawn_agent is in the agents tool category: spark.agents alone would
        # still be refused at the tool, so the power user holds both.
        assert permission_name("agents") in role_permissions(POWER_USER)

    @pytest.mark.parametrize("category", sorted(LOCAL_ONLY_CATEGORIES))
    def test_only_the_administrator_holds_local_only_tools(self, category: str) -> None:
        # A6 still refuses these categories on a server whatever the role; the
        # administrator holds them only because it holds every permission.
        holders = [n for n in ROLES if permission_name(category) in role_permissions(n)]
        assert holders == [ADMINISTRATOR]

    @pytest.mark.parametrize("role", [USER, POWER_USER, AUDITOR])
    def test_no_other_role_changes_instance_settings_or_users(self, role: str) -> None:
        held = role_permissions(role)
        assert "spark.settings.instance" not in held
        assert "spark.users.admin" not in held

    def test_unknown_role_has_no_permissions(self) -> None:
        assert role_permissions("no_such_role") == frozenset()


class TestRegistry:
    def test_builds_a_warden_registry(self) -> None:
        registry = build_registry()
        assert isinstance(registry, PermissionRegistry)
        assert registry.permissions() == dict(PERMISSIONS)
        assert {r.name for r in registry.roles()} == set(ROLES)

    def test_administrator_is_wardens_administrator_role(self) -> None:
        assert build_registry().administrator_roles() == {ADMINISTRATOR}

    @pytest.mark.parametrize("role", [ADMINISTRATOR, AUDITOR, POWER_USER, USER])
    def test_registry_roles_match_the_catalogue(self, role: str) -> None:
        assert build_registry().role(role).permissions == role_permissions(role)

    def test_registry_permissions_for_a_user_with_several_roles(self) -> None:
        registry = build_registry()
        assert registry.permissions_for([AUDITOR, USER]) == role_permissions(
            AUDITOR
        ) | role_permissions(USER)

    def test_a_new_role_entry_is_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        catalogue = dict(ROLES)
        catalogue["researcher"] = RoleSpec(
            frozenset({permission_name("web"), permission_name("knowledge")})
        )
        monkeypatch.setattr(permissions, "ROLES", catalogue)
        registry = build_registry()
        assert "researcher" in registry
        assert registry.role("researcher").permissions == {
            "spark.tools.web",
            "spark.tools.knowledge",
        }
        assert registry.role("researcher").administrator is False

    def test_a_role_with_an_undeclared_permission_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        catalogue = dict(ROLES)
        catalogue["broken"] = RoleSpec(frozenset({"spark.no_such_thing"}))
        monkeypatch.setattr(permissions, "ROLES", catalogue)
        with pytest.raises(ValueError):
            build_registry()

    def test_each_call_builds_a_fresh_registry(self) -> None:
        assert build_registry() is not build_registry()
