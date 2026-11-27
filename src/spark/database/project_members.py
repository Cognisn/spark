"""Who a top-level project is shared with (server mode; PC9, PC10).

``project_members`` holds one row per person a top-level project is shared with: their
role (``contributor`` or ``viewer``, shown as Read-only) and their scope (``project_only``
or ``all_conversations``). The owner (``projects.user_guid``) has no row. Only the owner
shares, changes and removes; a member may leave. Sub-projects are reached through their
top-level project and are never shared on their own.

A change that takes conversations away from someone (removal, leaving, a scope narrowed
to ``project_only``, a role lowered) reports which ones, so the endpoint can close their
open views and stop a turn of theirs. Removing a member also removes their individual
shares of the tree's conversations they did not start (under the share limit nobody
outside the project holds one) and gives back a turn they hold in one they no longer
reach; their resources stay in the project (PC11).

Every function takes the caller's guid and decides access through
:func:`spark.projects.access.require_project` first.
"""

from __future__ import annotations

from dataclasses import dataclass

from spark.database.scope import READABLE_CONVERSATIONS, NotPermitted
from spark.db.engine import Executor, Row
from spark.projects.access import (
    ProjectNeed,
    ProjectRefused,
    ProjectRole,
    ShareScope,
    project_access,
    require_project,
)

__all__ = [
    "MemberChange",
    "leave",
    "members",
    "remove",
    "share",
    "tree_conversations",
]

_SHAREABLE = (ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER)


@dataclass(frozen=True, slots=True)
class MemberChange:
    """What a membership change did: ``outcome`` is ``added``, ``changed``,
    ``unchanged`` or ``removed``; ``lost`` the conversations the person no longer reaches."""

    outcome: str
    previous_role: ProjectRole | None = None
    previous_scope: ShareScope | None = None
    lost: tuple[int, ...] = ()


async def tree_conversations(db: Executor, root_id: int) -> list[int]:
    """The active conversations of one project tree (deliberately unscoped: callers have
    decided access to the tree; never reachable from a request on its own)."""
    rows = await db.fetch_all(
        """SELECT c.id FROM conversations c JOIN projects p ON p.id = c.project_id
            WHERE p.root_id = ? AND c.is_active = 1 ORDER BY c.id""",
        (root_id,),
    )
    return [int(r["id"]) for r in rows]


async def _reached(db: Executor, user_guid: str, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    marks = ", ".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT id FROM conversations WHERE id IN ({marks}) "
        f"AND id IN ({READABLE_CONVERSATIONS})",
        (*ids, user_guid),
    )
    return {int(r["id"]) for r in rows}


async def _top_level(db: Executor, actor_guid: str, project_id: int) -> int:
    access = await require_project(db, actor_guid, project_id, ProjectNeed.MANAGE)
    if access.parent_id is not None:
        raise ProjectRefused("Only a top-level project is shared; share its top-level project.")
    return access.root_id


async def members(db: Executor, actor_guid: str, project_id: int) -> list[Row]:
    """The people a top-level project is shared with, by when they were added
    (``user_guid``, ``role``, ``scope``, ``added_by``, ``added_at``). The owner only."""
    access = await require_project(db, actor_guid, project_id, ProjectNeed.MANAGE)
    return await db.fetch_all(
        """SELECT user_guid, role, scope, added_by, added_at FROM project_members
            WHERE project_id = ? ORDER BY added_at, user_guid""",
        (access.root_id,),
    )


async def people(db: Executor, actor_guid: str, project_id: int) -> list[str]:
    """The guids of everyone who has a project's tree: its owner first, then each member
    by when they were added. Anyone who may view the project (an owner, contributor or
    viewer) may read it; it carries no role and no scope (PC9: the share picker's limit)."""
    access = await require_project(db, actor_guid, project_id, ProjectNeed.VIEW)
    owner = await db.fetch_value("SELECT user_guid FROM projects WHERE id = ?", (access.root_id,))
    rows = await db.fetch_all(
        """SELECT user_guid FROM project_members WHERE project_id = ?
            ORDER BY added_at, user_guid""",
        (access.root_id,),
    )
    return [str(owner)] + [str(r["user_guid"]) for r in rows if str(r["user_guid"]) != str(owner)]


async def share(
    db: Executor,
    actor_guid: str,
    project_id: int,
    user_guid: str,
    role: ProjectRole,
    scope: ShareScope,
) -> MemberChange:
    """Share a top-level project ``actor_guid`` owns with ``user_guid`` at ``role`` and
    ``scope``, or change their role or scope, in one transaction.

    Raises :class:`~spark.database.scope.NotOwned` /
    :class:`~spark.database.scope.NotPermitted` unless the actor owns it, and
    :class:`~spark.projects.access.ProjectRefused` for a sub-project, the owner role or
    the owner themself.
    """
    if role not in _SHAREABLE:
        raise ProjectRefused("A project is shared as Contributor or Read-only.")
    async with db.transaction() as conn:
        root = await _top_level(conn, actor_guid, project_id)
        if not user_guid or user_guid == actor_guid:
            raise ProjectRefused("The owner already has the project.")
        before_ids = await tree_conversations(conn, root)
        before = await _reached(conn, user_guid, before_ids)
        held = await conn.fetch_one(
            "SELECT role, scope FROM project_members WHERE project_id = ? AND user_guid = ?",
            (root, user_guid),
        )
        if held is None:
            await conn.execute(
                """INSERT INTO project_members (project_id, user_guid, role, scope, added_by)
                    VALUES (?, ?, ?, ?, ?)""",
                (root, user_guid, role.value, scope.value, actor_guid),
            )
            return MemberChange("added")
        previous_role, previous_scope = ProjectRole(held["role"]), ShareScope(held["scope"])
        if previous_role is role and previous_scope is scope:
            return MemberChange("unchanged", previous_role, previous_scope)
        await conn.execute(
            "UPDATE project_members SET role = ?, scope = ? WHERE project_id = ? AND user_guid = ?",
            (role.value, scope.value, root, user_guid),
        )
        after = await _reached(conn, user_guid, before_ids)
        await _release_turns(conn, user_guid, sorted(before - after))
        return MemberChange("changed", previous_role, previous_scope, tuple(sorted(before - after)))


async def _release_turns(conn: Executor, user_guid: str, ids: list[int]) -> None:
    if not ids:
        return
    marks = ", ".join("?" for _ in ids)
    await conn.execute(
        f"DELETE FROM conversation_turns WHERE user_guid = ? AND conversation_id IN ({marks})",
        (user_guid, *ids),
    )


async def _drop(conn: Executor, root: int, user_guid: str) -> MemberChange:
    held = await conn.fetch_one(
        "SELECT role, scope FROM project_members WHERE project_id = ? AND user_guid = ?",
        (root, user_guid),
    )
    if held is None:
        return MemberChange("unchanged")
    ids = await tree_conversations(conn, root)
    before = await _reached(conn, user_guid, ids)
    await conn.execute(
        "DELETE FROM project_members WHERE project_id = ? AND user_guid = ?", (root, user_guid)
    )
    # Their individual shares of the tree's conversations someone else started go too.
    if ids:
        marks = ", ".join("?" for _ in ids)
        await conn.execute(
            f"""DELETE FROM conversation_members WHERE user_guid = ? AND role <> 'owner'
                AND conversation_id IN ({marks})""",
            (user_guid, *ids),
        )
    after = await _reached(conn, user_guid, ids)
    lost = sorted(before - after)
    await _release_turns(conn, user_guid, lost)
    return MemberChange(
        "removed", ProjectRole(held["role"]), ShareScope(held["scope"]), tuple(lost)
    )


async def remove(db: Executor, actor_guid: str, project_id: int, user_guid: str) -> MemberChange:
    """Remove ``user_guid`` from a top-level project ``actor_guid`` owns. ``outcome`` is
    ``unchanged`` when they were not a member."""
    async with db.transaction() as conn:
        root = await _top_level(conn, actor_guid, project_id)
        return await _drop(conn, root, user_guid)


async def leave(db: Executor, user_guid: str, project_id: int) -> MemberChange:
    """``user_guid`` leaves a project shared with them. Raises
    :class:`~spark.database.scope.NotPermitted` for the owner (who transfers or deletes)."""
    async with db.transaction() as conn:
        access = await require_project(conn, user_guid, project_id, ProjectNeed.VIEW)
        if access.role is ProjectRole.OWNER:
            raise NotPermitted("the owner cannot leave their own project")
        return await _drop(conn, access.root_id, user_guid)


async def has_access(db: Executor, project_id: int, user_guid: str) -> bool:
    """Whether ``user_guid`` is the project's owner or a member of its tree."""
    return await project_access(db, user_guid, project_id) is not None
