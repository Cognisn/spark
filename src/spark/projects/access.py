"""The one access decision for projects (PC9, PC10).

A project belongs to its owner, ``projects.user_guid``, and so does every project of its
tree. Only top-level projects are shared: ``project_members`` names each person, their
role (``contributor`` or ``viewer``, shown as Read-only) and their scope
(``project_only`` or ``all_conversations``), and a sub-project is reached through its
top-level project (``root_id``). What each role may do is one table,
:data:`PROJECT_ALLOWS`:

* ``VIEW``: the project's description, instructions, resources, sub-projects, and the
  conversations the person can see;
* ``CONTRIBUTE``: start conversations anywhere in the tree and add resources (removing
  only their own);
* ``MANAGE``: edit, create, move and delete sub-projects, manage members, transfer and
  delete. The owner's alone.

A conversation in a project is decided by :func:`spark.database.scope.conversation_role`,
which this module's rules extend: the scope ``all_conversations`` shows every conversation
of the tree at the member's role, and the project's owner sees every conversation in it
(:data:`spark.database.scope.PROJECT_OWNER_ROLE`). A person who is not a member (or there
is no such project) gets :class:`~spark.database.scope.NotOwned`, a member without the
capability :class:`~spark.database.scope.NotPermitted`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from spark.database.scope import NotOwned, NotPermitted
from spark.db.engine import Executor

__all__ = [
    "MAX_DEPTH",
    "PROJECT_ALLOWS",
    "DepthLimit",
    "ProjectRefused",
    "ProjectAccess",
    "ProjectNeed",
    "ProjectRole",
    "ShareScope",
    "project_access",
    "require_project",
]

# Sub-projects nest up to five levels; the top-level project is level 1 (PC6).
MAX_DEPTH = 5


class ProjectRefused(ValueError):
    """What was asked cannot be done to the project; the message says why, for the person
    who asked (an endpoint answers 400 with it)."""


class DepthLimit(ProjectRefused):
    """Creating or moving would put a project below the fifth level."""

    def __init__(self) -> None:
        super().__init__(
            f"Projects nest at most {MAX_DEPTH} levels deep (the top-level project is "
            f"level 1), so this would go too deep."
        )


class ProjectRole(StrEnum):
    """A person's place in a project."""

    OWNER = "owner"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class ShareScope(StrEnum):
    """What a share of a project shows besides the project itself."""

    PROJECT_ONLY = "project_only"
    ALL_CONVERSATIONS = "all_conversations"


class ProjectNeed(StrEnum):
    """What a request needs to do in a project."""

    VIEW = "view"
    CONTRIBUTE = "contribute"
    MANAGE = "manage"


PROJECT_ALLOWS: dict[ProjectRole, frozenset[ProjectNeed]] = {
    ProjectRole.OWNER: frozenset({ProjectNeed.VIEW, ProjectNeed.CONTRIBUTE, ProjectNeed.MANAGE}),
    ProjectRole.CONTRIBUTOR: frozenset({ProjectNeed.VIEW, ProjectNeed.CONTRIBUTE}),
    ProjectRole.VIEWER: frozenset({ProjectNeed.VIEW}),
}


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """A project the user may reach, with their role (and scope, for a member)."""

    project: dict[str, Any]
    role: ProjectRole
    scope: ShareScope | None
    user_guid: str

    @property
    def id(self) -> int:
        return int(self.project["id"])

    @property
    def root_id(self) -> int:
        return int(self.project["root_id"])

    @property
    def parent_id(self) -> int | None:
        parent = self.project["parent_id"]
        return None if parent is None else int(parent)

    @property
    def depth(self) -> int:
        return int(self.project["depth"])

    @property
    def owner_guid(self) -> str:
        return str(self.project["user_guid"])

    def allows(self, need: ProjectNeed) -> bool:
        return need in PROJECT_ALLOWS[self.role]

    def may_remove_resource(self, added_by: str) -> bool:
        """The owner removes any resource; a Contributor only those they added."""
        if self.role is ProjectRole.OWNER:
            return True
        return self.role is ProjectRole.CONTRIBUTOR and added_by == self.user_guid


async def project_access(db: Executor, user_guid: str, project_id: int) -> ProjectAccess | None:
    """``user_guid``'s access to ``project_id``, or None (no such project, or not a member)."""
    if not user_guid:
        return None
    row = await db.fetch_one(
        """SELECT p.id, p.user_guid, p.parent_id, p.root_id, p.depth, p.name, p.description,
                p.instructions, p.created_at, p.updated_at,
                pm.role AS member_role, pm.scope AS member_scope
            FROM projects p
            LEFT JOIN project_members pm ON pm.project_id = p.root_id AND pm.user_guid = ?
            WHERE p.id = ?""",
        (user_guid, project_id),
    )
    if row is None:
        return None
    project = {k: v for k, v in row.items() if not k.startswith("member_")}
    if row["user_guid"] == user_guid:
        return ProjectAccess(project, ProjectRole.OWNER, None, user_guid)
    member = row["member_role"]
    if member not in (ProjectRole.CONTRIBUTOR.value, ProjectRole.VIEWER.value):
        return None
    return ProjectAccess(
        project, ProjectRole(member), ShareScope(str(row["member_scope"])), user_guid
    )


async def require_project(
    db: Executor, user_guid: str, project_id: int, need: ProjectNeed
) -> ProjectAccess:
    """``user_guid``'s access to ``project_id`` when it allows ``need``.

    Raises :class:`NotOwned` when they may not reach it (or it does not exist) and
    :class:`NotPermitted` when they may, without the capability.
    """
    access = await project_access(db, user_guid, project_id)
    if access is None:
        raise NotOwned(f"project {project_id} not found")
    if not access.allows(need):
        raise NotPermitted(f"project {project_id}: {access.role.value} may not {need.value}")
    return access
