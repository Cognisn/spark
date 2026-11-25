"""Projects and their sub-projects, conversations and resources (PC6 to PC15).

A project tree belongs to one owner (``projects.user_guid`` on every project of it). Each
function takes the caller's guid and decides access with
:func:`spark.projects.access.require_project` (projects) or
:func:`spark.database.scope.conversation_role` (conversations) before reading or changing
anything; a refusal is :class:`~spark.database.scope.NotOwned` (404),
:class:`~spark.database.scope.NotPermitted` (403) or
:class:`~spark.projects.access.ProjectRefused` (400, with the reason).

* **Levels.** A sub-project sits one level below its parent; level 5 is the deepest
  (:class:`~spark.projects.access.DepthLimit`), checked here and by the database.
* **Conversations.** ``conversations.project_id`` places a conversation. A Contributor
  starts one anywhere in the tree (:func:`place_conversation`); moving one
  (:func:`move_conversation`) needs ownership of both it and the destination, goes into a
  project, out to no project or between levels of one project, and reports first who
  would gain or lose sight of it (PC12).
* **Resources** are files in the project owner's area, linked by ``project_resources``
  with who added each (PC11); the upload itself is :class:`spark.files.area.FileArea`'s,
  charged to the owner.
* **Deletion** (:func:`delete_project`) keeps conversations (moved up, or out to no
  project) or deletes everything; resources and sub-projects go either way (PC13).
* **Transfer** (:func:`transfer_project`) gives the tree, its resource files and their
  usage to a member (PC14).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from spark.database.scope import (
    PROJECT_OWNER_ROLE,
    READABLE_CONVERSATIONS,
    NotOwned,
    NotPermitted,
    Role,
    conversation_role,
)
from spark.db.engine import Connection, Executor, Row
from spark.projects.access import (
    MAX_DEPTH,
    DepthLimit,
    ProjectAccess,
    ProjectNeed,
    ProjectRefused,
    ProjectRole,
    ShareScope,
    project_access,
    require_project,
)

__all__ = [
    "MAX_DESCRIPTION",
    "MAX_INSTRUCTIONS",
    "MAX_NAME",
    "DeleteMode",
    "DeletePreview",
    "DeleteResult",
    "MoveReport",
    "TransferQuota",
    "TransferResult",
    "add_notice",
    "add_resource",
    "conversation_resources",
    "create_project",
    "delete_preview",
    "delete_project",
    "list_projects",
    "list_resources",
    "mark_notices_seen",
    "move_conversation",
    "move_project",
    "notices",
    "path_of",
    "place_conversation",
    "project_conversations",
    "project_resource",
    "remove_resource",
    "resource_for_conversation",
    "sub_projects",
    "transfer_project",
    "update_project",
    "visible_conversations",
]

MAX_NAME = 200
MAX_DESCRIPTION = 2_000
MAX_INSTRUCTIONS = 50_000

_RANK = {Role.VIEWER: 1, Role.CONTRIBUTOR: 2, Role.OWNER: 3}


def _text(value: object, what: str, limit: int, *, required: bool = False) -> str:
    text = "" if value is None else str(value)
    if what == "name":
        text = " ".join(text.split())
    else:
        text = text.strip()
    if required and not text:
        raise ProjectRefused(f"A project needs a {what}.")
    if len(text) > limit:
        raise ProjectRefused(f"A project's {what} is at most {limit:,} characters.")
    return text


# -- Projects ------------------------------------------------------------------------------


async def create_project(
    db: Executor,
    user_guid: str,
    name: str,
    *,
    description: str = "",
    instructions: str = "",
    parent_id: int | None = None,
) -> int:
    """Create a top-level project for ``user_guid``, or a sub-project of ``parent_id`` (the
    owner's alone, at most level 5); returns its id."""
    clean = _text(name, "name", MAX_NAME, required=True)
    about = _text(description, "description", MAX_DESCRIPTION)
    rules = _text(instructions, "instructions", MAX_INSTRUCTIONS)
    async with db.transaction() as conn:
        depth, root, owner = 1, None, user_guid
        if parent_id is not None:
            parent = await require_project(conn, user_guid, parent_id, ProjectNeed.MANAGE)
            depth, root, owner = parent.depth + 1, parent.root_id, parent.owner_guid
            if depth > MAX_DEPTH:
                raise DepthLimit()
        new = int(
            await conn.fetch_value(
                """INSERT INTO projects
                    (user_guid, parent_id, root_id, depth, name, description, instructions)
                    VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (owner, parent_id, root, depth, clean, about, rules),
            )
        )
        if root is None:
            await conn.execute("UPDATE projects SET root_id = ? WHERE id = ?", (new, new))
    return new


async def update_project(
    db: Executor,
    user_guid: str,
    project_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
) -> list[str]:
    """Change a project's name, description or instructions (the owner's alone); returns
    the names of the fields that changed."""
    wanted: dict[str, str] = {}
    if name is not None:
        wanted["name"] = _text(name, "name", MAX_NAME, required=True)
    if description is not None:
        wanted["description"] = _text(description, "description", MAX_DESCRIPTION)
    if instructions is not None:
        wanted["instructions"] = _text(instructions, "instructions", MAX_INSTRUCTIONS)
    async with db.transaction() as conn:
        access = await require_project(conn, user_guid, project_id, ProjectNeed.MANAGE)
        changed = {k: v for k, v in wanted.items() if access.project[k] != v}
        if changed:
            sets = ", ".join(f"{k} = ?" for k in changed)
            await conn.execute(
                f"UPDATE projects SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (*changed.values(), project_id),
            )
    return sorted(changed)


async def _tree(conn: Executor, root_id: int) -> list[Row]:
    return await conn.fetch_all(
        "SELECT id, parent_id, depth, name, user_guid FROM projects WHERE root_id = ? ORDER BY id",
        (root_id,),
    )


def _subtree(rows: Sequence[Row], project_id: int) -> list[int]:
    """``project_id`` and every project below it, parents before children."""
    children: dict[int, list[int]] = {}
    for row in rows:
        if row["parent_id"] is not None:
            children.setdefault(int(row["parent_id"]), []).append(int(row["id"]))
    found, queue = [], [project_id]
    while queue:
        current = queue.pop(0)
        found.append(current)
        queue.extend(children.get(current, []))
    return found


async def path_of(conn: Executor, project_id: int) -> list[Row]:
    """The projects from the top level down to ``project_id`` (deliberately unscoped: the
    caller has decided access to ``project_id``)."""
    rows = await conn.fetch_all(
        """SELECT p.id, p.parent_id, p.depth, p.name, p.instructions, p.user_guid
            FROM projects p WHERE p.root_id = (SELECT root_id FROM projects WHERE id = ?)""",
        (project_id,),
    )
    by_id = {int(r["id"]): r for r in rows}
    chain: list[Row] = []
    current: int | None = project_id
    while current is not None and current in by_id:
        row = by_id[current]
        chain.append(row)
        current = None if row["parent_id"] is None else int(row["parent_id"])
    return list(reversed(chain))


async def move_project(
    db: Executor, user_guid: str, project_id: int, parent_id: int
) -> tuple[int, int]:
    """Move a sub-project (with everything below it) under another project of the same
    tree; the owner's alone. Returns (old parent id, new parent id)."""
    async with db.transaction() as conn:
        access = await require_project(conn, user_guid, project_id, ProjectNeed.MANAGE)
        if access.parent_id is None:
            raise ProjectRefused("A top-level project is not moved; move its sub-projects.")
        target = await require_project(conn, user_guid, parent_id, ProjectNeed.MANAGE)
        if target.root_id != access.root_id:
            raise ProjectRefused("A sub-project moves only within its own top-level project.")
        rows = await _tree(conn, access.root_id)
        below = _subtree(rows, project_id)
        if parent_id in below:
            raise ProjectRefused("A project cannot move into itself or one of its sub-projects.")
        shift = target.depth + 1 - access.depth
        deepest = max(int(r["depth"]) for r in rows if int(r["id"]) in below)
        if deepest + shift > MAX_DEPTH:
            raise DepthLimit()
        old_parent = access.parent_id
        if old_parent != parent_id:
            await conn.execute(
                "UPDATE projects SET parent_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (parent_id, project_id),
            )
            if shift:
                marks = ", ".join("?" for _ in below)
                await conn.execute(
                    f"UPDATE projects SET depth = depth + ? WHERE id IN ({marks})",
                    (shift, *below),
                )
    return old_parent, parent_id


async def list_projects(db: Executor, user_guid: str) -> list[Row]:
    """Every project the user may view, top levels first: their own and those of trees
    shared with them. Each row carries ``role`` (``owner``, ``contributor`` or ``viewer``)
    and ``scope`` (None for the owner)."""
    return await db.fetch_all(
        """SELECT p.id, p.parent_id, p.root_id, p.depth, p.name, p.description,
                p.user_guid AS owner_guid, p.created_at, p.updated_at,
                CASE WHEN p.user_guid = ? THEN 'owner' ELSE pm.role END AS role,
                CASE WHEN p.user_guid = ? THEN NULL ELSE pm.scope END AS scope
            FROM projects p
            LEFT JOIN project_members pm ON pm.project_id = p.root_id AND pm.user_guid = ?
            WHERE p.user_guid = ? OR pm.user_guid IS NOT NULL
            ORDER BY p.depth, p.name, p.id""",
        (user_guid, user_guid, user_guid, user_guid),
    )


async def sub_projects(db: Executor, user_guid: str, project_id: int) -> list[Row]:
    """The projects directly below ``project_id`` (VIEW needed), by name."""
    await require_project(db, user_guid, project_id, ProjectNeed.VIEW)
    return await db.fetch_all(
        """SELECT id, name, description, depth, updated_at FROM projects
            WHERE parent_id = ? ORDER BY name, id""",
        (project_id,),
    )


async def visible_conversations(
    db: Executor, user_guid: str, project_ids: Sequence[int] | None = None
) -> list[Row]:
    """The active conversations in projects the user may read (in ``project_ids`` only,
    when given), newest first: ``id``, ``name``, ``project_id``, ``owner_guid``,
    ``conversation_type``, ``created_at``, ``last_updated``."""
    where, params = "", list[object]([user_guid])
    if project_ids is not None:
        if not project_ids:
            return []
        where = f"AND c.project_id IN ({', '.join('?' for _ in project_ids)})"
        params += [int(p) for p in project_ids]
    return await db.fetch_all(
        f"""SELECT c.id, c.name, c.project_id, c.user_guid AS owner_guid,
                c.conversation_type, c.created_at, c.last_updated
            FROM conversations c
            WHERE c.project_id IS NOT NULL AND c.is_active = 1
            AND c.id IN ({READABLE_CONVERSATIONS}) {where}
            ORDER BY c.last_updated DESC, c.id DESC""",
        tuple(params),
    )


async def project_conversations(db: Executor, user_guid: str, project_id: int) -> list[Row]:
    """The conversations directly in ``project_id`` the user may read (VIEW needed)."""
    await require_project(db, user_guid, project_id, ProjectNeed.VIEW)
    return await visible_conversations(db, user_guid, [project_id])


# -- Conversations -------------------------------------------------------------------------


async def _conversation(conn: Executor, conversation_id: int) -> Row:
    row = await conn.fetch_one(
        "SELECT id, user_guid, project_id, conversation_type FROM conversations WHERE id = ?",
        (conversation_id,),
    )
    if row is None:
        raise NotOwned(f"conversation {conversation_id} not found")
    return row


async def _started_by(conn: Executor, user_guid: str, conversation_id: int) -> Row:
    """The conversation, when ``user_guid`` started it (owns it)."""
    role = await conversation_role(conn, user_guid, conversation_id)
    if role is None:
        raise NotOwned(f"conversation {conversation_id} not found")
    if role is not Role.OWNER:
        raise NotPermitted("Only the person who started a conversation moves it.")
    row = await _conversation(conn, conversation_id)
    if (row["conversation_type"] or "standard") != "standard":
        raise ProjectRefused("Debates and panels are not placed in projects.")
    return row


async def place_conversation(
    db: Executor, user_guid: str, conversation_id: int, project_id: int
) -> None:
    """Put a conversation ``user_guid`` has just started, with nothing said in it yet, into
    ``project_id`` (owner or Contributor). Moving one that is under way is
    :func:`move_conversation`'s."""
    async with db.transaction() as conn:
        row = await _started_by(conn, user_guid, conversation_id)
        await require_project(conn, user_guid, project_id, ProjectNeed.CONTRIBUTE)
        if row["project_id"] is not None:
            raise ProjectRefused("The conversation is already in a project.")
        said = await conn.fetch_value(
            "SELECT 1 FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
        others = await conn.fetch_value(
            "SELECT 1 FROM conversation_members WHERE conversation_id = ? AND role <> 'owner'",
            (conversation_id,),
        )
        if said is not None or others is not None:
            raise ProjectRefused("Only a new conversation is started in a project; move it.")
        await conn.execute(
            "UPDATE conversations SET project_id = ? WHERE id = ?", (project_id, conversation_id)
        )


async def _project_audience(conn: Executor, project_id: int | None) -> dict[str, Role]:
    """Who sees a conversation through ``project_id``, besides whoever started it."""
    if project_id is None:
        return {}
    owner = await conn.fetch_one(
        "SELECT user_guid, root_id FROM projects WHERE id = ?", (project_id,)
    )
    if owner is None:
        return {}
    found = {str(owner["user_guid"]): PROJECT_OWNER_ROLE}
    for row in await conn.fetch_all(
        """SELECT user_guid, role FROM project_members
            WHERE project_id = ? AND scope = 'all_conversations'""",
        (owner["root_id"],),
    ):
        found[str(row["user_guid"])] = Role(row["role"])
    return found


async def _tree_people(conn: Executor, project_id: int) -> set[str]:
    """The owner and every member of ``project_id``'s tree."""
    row = await conn.fetch_one(
        "SELECT user_guid, root_id FROM projects WHERE id = ?", (project_id,)
    )
    if row is None:
        return set()
    people = {str(row["user_guid"])}
    for member in await conn.fetch_all(
        "SELECT user_guid FROM project_members WHERE project_id = ?", (row["root_id"],)
    ):
        people.add(str(member["user_guid"]))
    return people


def _merge(*sources: dict[str, Role]) -> dict[str, Role]:
    merged: dict[str, Role] = {}
    for source in sources:
        for guid, role in source.items():
            if guid not in merged or _RANK[role] > _RANK[merged[guid]]:
                merged[guid] = role
    return merged


@dataclass(slots=True)
class MoveReport:
    """What a move does to who sees the conversation (PC12).

    ``widened``: people who would see it, or gain a role, ``{guid: role}``; ``narrowed``:
    people who would lose sight of it or a role, ``{guid: role after or None}``;
    ``shares_removed``: individual shares that end because the person has no access to
    the destination project. ``confirm_required`` is set, and nothing moved, when either
    list is not empty and the request did not confirm.
    """

    conversation_id: int
    from_project: int | None
    to_project: int | None
    widened: dict[str, str] = field(default_factory=dict)
    narrowed: dict[str, str | None] = field(default_factory=dict)
    shares_removed: list[str] = field(default_factory=list)
    moved: bool = False
    confirm_required: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "from_project": self.from_project,
            "to_project": self.to_project,
            "widened": dict(self.widened),
            "narrowed": dict(self.narrowed),
            "shares_removed": list(self.shares_removed),
            "moved": self.moved,
            "confirm_required": self.confirm_required,
        }


async def move_conversation(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    project_id: int | None,
    *,
    confirm: bool = False,
) -> MoveReport:
    """Move a conversation into a project, out to no project (``project_id`` None) or
    between levels of its project (PC12).

    The mover must have started the conversation and own the destination project. A
    conversation in one project reaches another only through no project. When the move
    would show it to more people or hide it from people who see it now, nothing moves
    unless ``confirm`` is set: the report says who (``confirm_required``).
    """
    async with db.transaction() as conn:
        row = await _started_by(conn, user_guid, conversation_id)
        current = None if row["project_id"] is None else int(row["project_id"])
        report = MoveReport(conversation_id, current, project_id)
        if project_id == current:
            raise ProjectRefused("The conversation is already there.")
        if project_id is not None:
            target = await require_project(conn, user_guid, project_id, ProjectNeed.VIEW)
            if target.role is not ProjectRole.OWNER:
                raise NotPermitted("A conversation moves only into a project you own.")
            if current is not None:
                here = await conn.fetch_value(
                    "SELECT root_id FROM projects WHERE id = ?", (current,)
                )
                if here is not None and int(here) != target.root_id:
                    raise ProjectRefused(
                        "A conversation moves between projects through no project: "
                        "move it out of its project first."
                    )
        individual = {
            str(r["user_guid"]): Role(r["role"])
            for r in await conn.fetch_all(
                """SELECT user_guid, role FROM conversation_members
                    WHERE conversation_id = ? AND role <> 'owner'""",
                (conversation_id,),
            )
        }
        kept = dict(individual)
        if project_id is not None:
            allowed = await _tree_people(conn, project_id)
            report.shares_removed = sorted(g for g in individual if g not in allowed)
            kept = {g: r for g, r in individual.items() if g in allowed}
        starter = str(row["user_guid"])
        before = _merge(individual, await _project_audience(conn, current))
        after = _merge(kept, await _project_audience(conn, project_id))
        before.pop(starter, None)
        after.pop(starter, None)
        for guid, role in after.items():
            if guid not in before or _RANK[role] > _RANK[before[guid]]:
                report.widened[guid] = role.value
        for guid, role in before.items():
            now = after.get(guid)
            if now is None or _RANK[now] < _RANK[role]:
                report.narrowed[guid] = None if now is None else now.value
        if (report.widened or report.narrowed) and not confirm:
            report.confirm_required = True
            return report
        await conn.execute(
            "UPDATE conversations SET project_id = ? WHERE id = ?", (project_id, conversation_id)
        )
        for guid in report.shares_removed:
            await conn.execute(
                "DELETE FROM conversation_members WHERE conversation_id = ? AND user_guid = ?"
                " AND role <> 'owner'",
                (conversation_id, guid),
            )
        for guid in report.narrowed:
            if report.narrowed[guid] is None:
                await conn.execute(
                    "DELETE FROM conversation_turns WHERE conversation_id = ? AND user_guid = ?",
                    (conversation_id, guid),
                )
        report.moved = True
    return report


# -- Resources -----------------------------------------------------------------------------

_RESOURCE_COLUMNS = """r.project_id, r.file_id, r.added_by, r.added_at, f.filename, f.mime_type,
    f.size_bytes, f.storage_key, f.user_guid AS owner_guid, f.created_at, f.source"""


async def add_resource(db: Executor, user_guid: str, project_id: int, file_id: int) -> None:
    """Make a file in the project owner's area a resource of ``project_id`` (owner or
    Contributor), recording who added it."""
    async with db.transaction() as conn:
        access = await require_project(conn, user_guid, project_id, ProjectNeed.CONTRIBUTE)
        owned = await conn.fetch_value(
            "SELECT 1 FROM user_files WHERE id = ? AND user_guid = ?",
            (file_id, access.owner_guid),
        )
        if owned is None:
            raise ProjectRefused("A resource is a file in the project owner's file area.")
        added = await conn.fetch_value(
            """INSERT INTO project_resources (project_id, file_id, added_by) VALUES (?, ?, ?)
                ON CONFLICT (project_id, file_id) DO NOTHING RETURNING id""",
            (project_id, file_id, user_guid),
        )
        if added is None:
            raise ProjectRefused("That file is already a resource of the project.")


async def list_resources(db: Executor, user_guid: str, project_id: int) -> list[Row]:
    """The resources of one project (not those it inherits), oldest first (VIEW needed)."""
    await require_project(db, user_guid, project_id, ProjectNeed.VIEW)
    return await db.fetch_all(
        f"""SELECT {_RESOURCE_COLUMNS} FROM project_resources r
            JOIN user_files f ON f.id = r.file_id
            WHERE r.project_id = ? ORDER BY r.added_at, r.id""",
        (project_id,),
    )


async def project_resource(
    db: Executor, user_guid: str, project_id: int, file_id: int
) -> Row | None:
    """One resource of ``project_id`` the user may view, with its file, or None."""
    try:
        await require_project(db, user_guid, project_id, ProjectNeed.VIEW)
    except NotOwned:
        return None
    return await db.fetch_one(
        f"""SELECT {_RESOURCE_COLUMNS} FROM project_resources r
            JOIN user_files f ON f.id = r.file_id
            WHERE r.project_id = ? AND r.file_id = ?""",
        (project_id, file_id),
    )


async def _release(conn: Connection, owner: str, rows: Sequence[Row]) -> list[Row]:
    """Delete the files ``rows`` name from ``owner``'s area metadata, releasing their size;
    returns the deleted rows (their storage is the caller's to remove)."""
    gone: list[Row] = []
    if not rows:
        return gone
    await conn.execute(
        "INSERT INTO user_file_usage (user_guid, bytes_used) VALUES (?, 0) "
        "ON CONFLICT (user_guid) DO NOTHING",
        (owner,),
    )
    await conn.execute(
        "UPDATE user_file_usage SET bytes_used = bytes_used WHERE user_guid = ?", (owner,)
    )
    for row in rows:
        deleted = await conn.fetch_one(
            "DELETE FROM user_files WHERE id = ? AND user_guid = ? RETURNING *",
            (int(row["file_id"]), owner),
        )
        if deleted is not None:
            await conn.execute(
                "UPDATE user_file_usage SET bytes_used = bytes_used - ? WHERE user_guid = ?",
                (int(deleted["size_bytes"]), owner),
            )
            gone.append(deleted)
    return gone


async def remove_resource(
    db: Executor, user_guid: str, project_id: int, file_id: int
) -> Row | None:
    """Remove a resource: the owner any, a Contributor those they added. A file uploaded
    as a resource and linked nowhere else is deleted from the owner's area (its size
    released): its row is returned so its storage can be removed. Raises NotOwned when
    the project has no such resource."""
    async with db.transaction() as conn:
        access = await require_project(conn, user_guid, project_id, ProjectNeed.VIEW)
        row = await conn.fetch_one(
            f"""SELECT {_RESOURCE_COLUMNS} FROM project_resources r
                JOIN user_files f ON f.id = r.file_id
                WHERE r.project_id = ? AND r.file_id = ?""",
            (project_id, file_id),
        )
        if row is None:
            raise NotOwned(f"resource {file_id} not found")
        if not access.may_remove_resource(str(row["added_by"])):
            raise NotPermitted("Only the project's owner or whoever added it removes a resource.")
        await conn.execute(
            "DELETE FROM project_resources WHERE project_id = ? AND file_id = ?",
            (project_id, file_id),
        )
        elsewhere = await conn.fetch_value(
            "SELECT 1 FROM project_resources WHERE file_id = ?", (file_id,)
        )
        if elsewhere is not None or row["source"] != "project":
            return None
        gone = await _release(conn, access.owner_guid, [row])
        return gone[0] if gone else None


@dataclass(frozen=True, slots=True)
class LevelResources:
    """One level of a conversation's project path and the resources it holds."""

    project_id: int
    name: str
    level: int
    instructions: str
    resources: tuple[Row, ...]


async def conversation_resources(
    db: Executor, user_guid: str, conversation_id: int
) -> list[LevelResources]:
    """The project path of a conversation the user may read, top level first, each level
    with its instructions and resources.

    Empty for a conversation in no project, and for someone without access to its project:
    a member removed from a project keeps the conversations they started there, but not
    the project's instructions or resources."""
    role = await conversation_role(db, user_guid, conversation_id)
    if role is None:
        raise NotOwned(f"conversation {conversation_id} not found")
    row = await _conversation(db, conversation_id)
    if row["project_id"] is None:
        return []
    if await project_access(db, user_guid, int(row["project_id"])) is None:
        return []
    chain = await path_of(db, int(row["project_id"]))
    if not chain:
        return []
    ids = [int(p["id"]) for p in chain]
    marks = ", ".join("?" for _ in ids)
    found = await db.fetch_all(
        f"""SELECT {_RESOURCE_COLUMNS} FROM project_resources r
            JOIN user_files f ON f.id = r.file_id
            WHERE r.project_id IN ({marks}) ORDER BY r.added_at, r.id""",
        tuple(ids),
    )
    return [
        LevelResources(
            int(p["id"]),
            str(p["name"]),
            int(p["depth"]),
            str(p["instructions"] or ""),
            tuple(r for r in found if int(r["project_id"]) == int(p["id"])),
        )
        for p in chain
    ]


async def resource_for_conversation(
    db: Executor, user_guid: str, conversation_id: int, file_id: int
) -> Row | None:
    """A resource inherited by a conversation the user may read (any level of its path),
    with its file; None for anything else (another project's resource included)."""
    try:
        levels = await conversation_resources(db, user_guid, conversation_id)
    except NotOwned:
        return None
    for level in levels:
        for resource in level.resources:
            if int(resource["file_id"]) == int(file_id):
                return resource
    return None


# -- Deletion ------------------------------------------------------------------------------


class DeleteMode(StrEnum):
    """What happens to a deleted project's conversations (PC13)."""

    KEEP = "keep"
    EVERYTHING = "everything"


@dataclass(frozen=True, slots=True)
class DeletePreview:
    """What deleting a project affects, for the confirmation."""

    name: str
    sub_projects: int
    conversations: int
    conversations_by_others: int
    resources: int
    resource_bytes: int
    members: int
    people_told: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sub_projects": self.sub_projects,
            "conversations": self.conversations,
            "conversations_by_others": self.conversations_by_others,
            "resources": self.resources,
            "resource_bytes": self.resource_bytes,
            "members": self.members,
            "people_told": len(self.people_told),
        }


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """What a deletion did: the files to remove from the owner's storage, the
    conversations kept or deleted, and the people told (guid: how many of theirs)."""

    name: str
    owner_guid: str
    mode: DeleteMode
    files: tuple[Row, ...]
    conversations_kept: tuple[int, ...]
    conversations_deleted: tuple[int, ...]
    told: dict[str, int]
    sub_projects: int
    resources: int


async def _deletion(
    conn: Executor, user_guid: str, project_id: int
) -> tuple[ProjectAccess, list[int], list[Row], list[Row]]:
    access = await require_project(conn, user_guid, project_id, ProjectNeed.MANAGE)
    below = _subtree(await _tree(conn, access.root_id), project_id)
    marks = ", ".join("?" for _ in below)
    talks = await conn.fetch_all(
        f"""SELECT id, user_guid FROM conversations
            WHERE project_id IN ({marks}) AND is_active = 1 ORDER BY id""",
        tuple(below),
    )
    resources = await conn.fetch_all(
        f"""SELECT {_RESOURCE_COLUMNS} FROM project_resources r
            JOIN user_files f ON f.id = r.file_id WHERE r.project_id IN ({marks})""",
        tuple(below),
    )
    return access, below, list(talks), list(resources)


async def delete_preview(db: Executor, user_guid: str, project_id: int) -> DeletePreview:
    """The counts a deletion's confirmation names (the owner's alone)."""
    access, below, talks, resources = await _deletion(db, user_guid, project_id)
    members = await db.fetch_value(
        "SELECT COUNT(*) FROM project_members WHERE project_id = ?", (access.root_id,)
    )
    others = [str(t["user_guid"]) for t in talks if t["user_guid"] != access.owner_guid]
    files = {int(r["file_id"]): int(r["size_bytes"]) for r in resources}
    return DeletePreview(
        name=str(access.project["name"]),
        sub_projects=len(below) - 1,
        conversations=len(talks),
        conversations_by_others=len(others),
        resources=len(resources),
        resource_bytes=sum(files.values()),
        members=int(members or 0),
        people_told=tuple(sorted(set(others))),
    )


async def delete_project(
    db: Executor, user_guid: str, project_id: int, mode: DeleteMode
) -> DeleteResult:
    """Delete a project, its sub-projects and their resources (the owner's alone).

    ``keep``: a sub-project's conversations (those of the projects below it too) move up
    to its parent; a top-level project's leave to no project, private again to whoever
    started each (individual shares stay). ``everything``: every conversation of the
    deleted projects is deleted, whoever started it, and each other starter is told.
    Resource files uploaded as resources and linked nowhere else are deleted from the
    owner's area and their size released; their rows are returned for the storage.
    """
    async with db.transaction() as conn:
        access, below, talks, resources = await _deletion(conn, user_guid, project_id)
        ids = [int(t["id"]) for t in talks]
        told: dict[str, int] = {}
        if ids:
            marks = ", ".join("?" for _ in ids)
            if mode is DeleteMode.KEEP:
                await conn.execute(
                    f"UPDATE conversations SET project_id = ? WHERE id IN ({marks})",
                    (access.parent_id, *ids),
                )
            else:
                await conn.execute(
                    "UPDATE conversations SET is_active = 0, project_id = NULL, "
                    f"last_updated = CURRENT_TIMESTAMP WHERE id IN ({marks})",
                    tuple(ids),
                )
                await conn.execute(
                    f"DELETE FROM conversation_turns WHERE conversation_id IN ({marks})",
                    tuple(ids),
                )
                for talk in talks:
                    starter = str(talk["user_guid"])
                    if starter != access.owner_guid:
                        told[starter] = told.get(starter, 0) + 1
        marks = ", ".join("?" for _ in below)
        deletable = []
        for row in {int(r["file_id"]): r for r in resources}.values():
            elsewhere = await conn.fetch_value(
                f"SELECT 1 FROM project_resources WHERE file_id = ? AND project_id NOT IN ({marks})",
                (int(row["file_id"]), *below),
            )
            if elsewhere is None and row["source"] == "project":
                deletable.append(row)
        await conn.execute(
            f"DELETE FROM project_resources WHERE project_id IN ({marks})", tuple(below)
        )
        gone = await _release(conn, access.owner_guid, deletable)
        name = str(access.project["name"])
        for guid, count in told.items():
            await add_notice(conn, guid, "project_deleted", name, project_id, user_guid, count)
        # Children first, so no foreign key ever names a deleted parent.
        for pid in reversed(below):
            await conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    return DeleteResult(
        name=name,
        owner_guid=access.owner_guid,
        mode=mode,
        files=tuple(gone),
        conversations_kept=tuple(ids) if mode is DeleteMode.KEEP else (),
        conversations_deleted=tuple(ids) if mode is DeleteMode.EVERYTHING else (),
        told=told,
        sub_projects=len(below) - 1,
        resources=len(resources),
    )


# -- Transfer ------------------------------------------------------------------------------


class TransferQuota(ProjectRefused):
    """The new owner lacks the storage quota for the project's resources."""


@dataclass(frozen=True, slots=True)
class TransferResult:
    """What a transfer moved: the tree's top-level id, its name and the resource files
    (``id``, ``storage_key``, ``size_bytes``) now in the new owner's area."""

    root_id: int
    name: str
    files: tuple[Row, ...]
    added_member: bool


CopyFiles = Callable[[Sequence[Row]], Awaitable[None]]


async def _lock_usage(conn: Executor, user_guid: str) -> int:
    await conn.execute(
        "INSERT INTO user_file_usage (user_guid, bytes_used) VALUES (?, 0) "
        "ON CONFLICT (user_guid) DO NOTHING",
        (user_guid,),
    )
    await conn.execute(
        "UPDATE user_file_usage SET bytes_used = bytes_used WHERE user_guid = ?", (user_guid,)
    )
    return int(
        await conn.fetch_value(
            "SELECT bytes_used FROM user_file_usage WHERE user_guid = ?", (user_guid,)
        )
    )


async def transfer_project(
    db: Executor,
    actor_guid: str,
    project_id: int,
    new_owner: str,
    *,
    quota_bytes: int | None,
    add_member: bool = False,
    copy_files: CopyFiles | None = None,
) -> TransferResult:
    """Make ``new_owner`` the owner of a top-level project ``actor_guid`` owns (PC14).

    The new owner must be a member, or is added in the same step when ``add_member`` is
    set. They take the tree, its resource files and their usage, refused with
    :class:`TransferQuota` when it would take them over ``quota_bytes``. The old owner
    stays as a Contributor with ``all_conversations``; conversations stay with whoever
    started them. ``copy_files`` is awaited with the files before anything commits (the
    caller copies their content into the new owner's area); if it raises, nothing changes.
    The new owner is told (a notice).
    """
    async with db.transaction() as conn:
        access = await require_project(conn, actor_guid, project_id, ProjectNeed.MANAGE)
        if access.parent_id is not None:
            raise ProjectRefused("Only a top-level project is transferred.")
        if not new_owner or new_owner == actor_guid:
            raise ProjectRefused("Transfer the project to someone else.")
        root = access.root_id
        member = await conn.fetch_value(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_guid = ?",
            (root, new_owner),
        )
        if member is None and not add_member:
            raise ProjectRefused("The new owner must be a member of the project.")
        files = await conn.fetch_all(
            """SELECT DISTINCT f.id, f.storage_key, f.size_bytes FROM project_resources r
                JOIN projects p ON p.id = r.project_id
                JOIN user_files f ON f.id = r.file_id
                WHERE p.root_id = ? AND f.user_guid = ? ORDER BY f.id""",
            (root, actor_guid),
        )
        total = sum(int(f["size_bytes"]) for f in files)
        used = await _lock_usage(conn, new_owner)
        await _lock_usage(conn, actor_guid)
        if quota_bytes is not None and total and used + total > quota_bytes:
            raise TransferQuota(
                f"The new owner does not have the storage for the project's resources: "
                f"{used:,} of {quota_bytes:,} bytes are in use and {total:,} more are needed."
            )
        ids = [int(f["id"]) for f in files]
        if ids:
            marks = ", ".join("?" for _ in ids)
            clash = await conn.fetch_value(
                f"""SELECT 1 FROM user_files WHERE user_guid = ? AND storage_key IN
                    (SELECT storage_key FROM user_files WHERE id IN ({marks}))""",
                (new_owner, *ids),
            )
            if clash is not None:
                raise ProjectRefused("The new owner already holds a file under the same key.")
            await conn.execute(
                f"UPDATE user_files SET user_guid = ? WHERE id IN ({marks})", (new_owner, *ids)
            )
            await conn.execute(
                "UPDATE user_file_usage SET bytes_used = bytes_used - ? WHERE user_guid = ?",
                (total, actor_guid),
            )
            await conn.execute(
                "UPDATE user_file_usage SET bytes_used = bytes_used + ? WHERE user_guid = ?",
                (total, new_owner),
            )
        await conn.execute(
            "UPDATE projects SET user_guid = ?, updated_at = CURRENT_TIMESTAMP WHERE root_id = ?",
            (new_owner, root),
        )
        await conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_guid = ?",
            (root, new_owner),
        )
        await conn.execute(
            """INSERT INTO project_members (project_id, user_guid, role, scope, added_by)
                VALUES (?, ?, ?, ?, ?)""",
            (
                root,
                actor_guid,
                ProjectRole.CONTRIBUTOR.value,
                ShareScope.ALL_CONVERSATIONS.value,
                actor_guid,
            ),
        )
        name = str(access.project["name"])
        await add_notice(conn, new_owner, "project_transferred", name, root, actor_guid, 0)
        if copy_files is not None and files:
            await copy_files(files)
    return TransferResult(root, name, tuple(files), member is None)


# -- Notices -------------------------------------------------------------------------------


async def add_notice(
    conn: Executor,
    user_guid: str,
    kind: str,
    subject: str,
    project_id: int | None,
    actor_guid: str | None,
    count: int,
) -> None:
    """Tell ``user_guid`` something about a project (shown in SPARK until seen)."""
    await conn.execute(
        """INSERT INTO project_notices (user_guid, kind, subject, project_id, actor_guid, count)
            VALUES (?, ?, ?, ?, ?, ?)""",
        (user_guid, kind, subject[:MAX_NAME], project_id, actor_guid, count),
    )


async def notices(db: Executor, user_guid: str, *, unseen_only: bool = True) -> list[Row]:
    """The user's project notices, newest first."""
    where = " AND seen_at IS NULL" if unseen_only else ""
    return await db.fetch_all(
        f"""SELECT id, kind, subject, project_id, actor_guid, count, created_at, seen_at
            FROM project_notices WHERE user_guid = ?{where} ORDER BY id DESC LIMIT 100""",
        (user_guid,),
    )


async def mark_notices_seen(db: Executor, user_guid: str) -> int:
    """Mark every notice of the user seen; returns how many."""
    rows = await db.fetch_all(
        """UPDATE project_notices SET seen_at = CURRENT_TIMESTAMP
            WHERE user_guid = ? AND seen_at IS NULL RETURNING id""",
        (user_guid,),
    )
    return len(rows)
