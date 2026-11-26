"""Who shares a conversation, and whose turn is running (server mode).

``conversation_members`` holds one row per person with access: the owner (kept in
step with ``conversations.user_guid``), contributors and viewers. Only the owner
shares, changes roles, removes people and transfers; anyone but the owner may
leave. A member's ``last_seen_at`` is NULL until they open a conversation shared
with them (the "new" marker), and ``is_favourite`` is their own star.

``conversation_turns`` is the turn lock: one participant's turn at a time, across
every web replica. A turn takes a lease row (:func:`acquire_turn`), refreshes it
while it runs (:func:`heartbeat_turn`) and gives it back when it ends
(:func:`release_turn`). A lease whose heartbeat is older than ``stale_after``
seconds belongs to a replica that died, and the next turn takes it over.

Every function takes the caller's guid and checks the caller's access through
:mod:`spark.database.scope` before reading or changing anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from spark.database.scope import (
    READABLE_CONVERSATIONS,
    Capability,
    NotOwned,
    NotPermitted,
    Role,
    require_access,
)
from spark.db.engine import Executor, Row

__all__ = [
    "HEARTBEAT_SECONDS",
    "STALE_AFTER_SECONDS",
    "MemberChange",
    "acquire_turn",
    "current_turn",
    "heartbeat_turn",
    "is_shared",
    "leave",
    "mark_seen",
    "members",
    "release_turn",
    "remove_member",
    "set_favourite",
    "share",
    "shared_with",
    "transfer",
    "unseen_count",
]

HEARTBEAT_SECONDS = 15
STALE_AFTER_SECONDS = 45

_SHAREABLE = (Role.CONTRIBUTOR, Role.VIEWER)


def _now() -> str:
    """UTC now as naive ISO text, comparable as text (SQLite) and as a timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


@dataclass(frozen=True, slots=True)
class MemberChange:
    """What :func:`share` did: ``added``, ``changed`` (role) or ``unchanged``."""

    outcome: str
    previous_role: Role | None


async def members(db: Executor, user_guid: str, conversation_id: int) -> list[Row]:
    """The people with access to a conversation the user may read, owner first.

    Each row: ``user_guid``, ``role``, ``added_by``, ``added_at``, ``last_seen_at``.
    Empty when the user may not read the conversation.
    """
    return await db.fetch_all(
        f"""SELECT user_guid, role, added_by, added_at, last_seen_at
            FROM conversation_members
            WHERE conversation_id = ? AND conversation_id IN ({READABLE_CONVERSATIONS})
            ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, added_at, user_guid""",
        (conversation_id, user_guid),
    )


async def is_shared(db: Executor, user_guid: str, conversation_id: int) -> bool:
    """Whether a conversation the user may read has anyone besides its owner: a member,
    or someone who sees it through its project (the project's owner when someone else
    started it, or a member of the project with the scope ``all_conversations``)."""
    found = await db.fetch_value(
        f"""SELECT 1 FROM conversations c WHERE c.id = ?
            AND c.id IN ({READABLE_CONVERSATIONS})
            AND (EXISTS (SELECT 1 FROM conversation_members cm
                    WHERE cm.conversation_id = c.id AND cm.role <> 'owner')
                OR EXISTS (SELECT 1 FROM projects p WHERE p.id = c.project_id
                    AND p.user_guid <> c.user_guid)
                OR EXISTS (SELECT 1 FROM projects p
                    JOIN project_members pm ON pm.project_id = p.root_id
                    WHERE p.id = c.project_id AND pm.scope = 'all_conversations'))""",
        (conversation_id, user_guid),
    )
    return found is not None


async def share(
    db: Executor, actor_guid: str, conversation_id: int, user_guid: str, role: Role
) -> MemberChange:
    """Give ``user_guid`` ``role`` (contributor or viewer) in a conversation ``actor_guid``
    owns, adding them or changing their role, in one transaction.

    Raises :class:`NotOwned` / :class:`NotPermitted` unless the actor owns it, and
    ``ValueError`` for the owner role or for the owner themself.
    """
    if role not in _SHAREABLE:
        raise ValueError("a conversation is shared as contributor or viewer")
    async with db.transaction() as conn:
        await require_access(conn, actor_guid, conversation_id, Capability.MANAGE)
        owner = await conn.fetch_value(
            "SELECT user_guid FROM conversations WHERE id = ?", (conversation_id,)
        )
        if user_guid == owner:
            raise ValueError("the owner already has the conversation")
        await _within_project(conn, conversation_id, user_guid)
        previous = await conn.fetch_value(
            "SELECT role FROM conversation_members WHERE conversation_id = ? AND user_guid = ?",
            (conversation_id, user_guid),
        )
        if previous is None:
            await conn.execute(
                """INSERT INTO conversation_members
                    (conversation_id, user_guid, role, added_by, added_at)
                    VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, user_guid, role.value, actor_guid, _now()),
            )
            return MemberChange("added", None)
        if previous == role.value:
            return MemberChange("unchanged", Role(previous))
        await conn.execute(
            "UPDATE conversation_members SET role = ? WHERE conversation_id = ? AND user_guid = ?",
            (role.value, conversation_id, user_guid),
        )
        return MemberChange("changed", Role(previous))


OUTSIDE_PROJECT = (
    "This conversation is in a project: share it only with people who have access to "
    "the project."
)


async def _within_project(conn: Executor, conversation_id: int, user_guid: str) -> None:
    """The share limit (PC9): a conversation in a project is shared, or transferred, only
    to the project's owner or a member of its tree. Raises ``ValueError`` otherwise."""
    allowed = await conn.fetch_value(
        """SELECT CASE WHEN c.project_id IS NULL THEN 1
                WHEN p.user_guid = ? THEN 1
                WHEN EXISTS (SELECT 1 FROM project_members pm
                    WHERE pm.project_id = p.root_id AND pm.user_guid = ?) THEN 1
                ELSE 0 END
            FROM conversations c LEFT JOIN projects p ON p.id = c.project_id
            WHERE c.id = ?""",
        (user_guid, user_guid, conversation_id),
    )
    if not allowed:
        raise ValueError(OUTSIDE_PROJECT)


async def remove_member(
    db: Executor, actor_guid: str, conversation_id: int, user_guid: str
) -> Role | None:
    """Remove ``user_guid`` from a conversation ``actor_guid`` owns. Returns the role they
    held (None: they were not a member). Their turn, if they hold it, is released.

    The owner cannot be removed (transfer first). Raises :class:`NotOwned` /
    :class:`NotPermitted` unless the actor owns the conversation.
    """
    async with db.transaction() as conn:
        await require_access(conn, actor_guid, conversation_id, Capability.MANAGE)
        return await _drop(conn, conversation_id, user_guid)


async def leave(db: Executor, user_guid: str, conversation_id: int) -> Role:
    """``user_guid`` leaves a conversation shared with them. Returns the role they held.

    Raises :class:`NotOwned` when they are not a member and :class:`NotPermitted`
    for the owner (who transfers or deletes instead).
    """
    async with db.transaction() as conn:
        role = await require_access(conn, user_guid, conversation_id, Capability.READ)
        if role is Role.OWNER:
            raise NotPermitted("the owner cannot leave their own conversation")
        await _drop(conn, conversation_id, user_guid)
        return role


async def _drop(conn: Executor, conversation_id: int, user_guid: str) -> Role | None:
    previous = await conn.fetch_value(
        "SELECT role FROM conversation_members WHERE conversation_id = ? AND user_guid = ?",
        (conversation_id, user_guid),
    )
    if previous is None:
        return None
    if previous == Role.OWNER.value:
        raise ValueError("the owner cannot be removed; transfer the conversation first")
    await conn.execute(
        "DELETE FROM conversation_members WHERE conversation_id = ? AND user_guid = ?",
        (conversation_id, user_guid),
    )
    await conn.execute(
        "DELETE FROM conversation_turns WHERE conversation_id = ? AND user_guid = ?",
        (conversation_id, user_guid),
    )
    return Role(previous)


async def transfer(db: Executor, actor_guid: str, conversation_id: int, new_owner: str) -> None:
    """Make ``new_owner`` the owner of a conversation ``actor_guid`` owns, in one transaction.

    The new owner must already have access, or is added in the transfer. The old owner
    becomes a contributor. ``conversations.user_guid`` and the owner rows move together.
    Links between this conversation and the old owner's others are removed (linked
    conversations stay private to their owner); knowledge graph builds and actions stay
    with the old owner. Each person keeps their own star.
    """
    async with db.transaction() as conn:
        await require_access(conn, actor_guid, conversation_id, Capability.MANAGE)
        if new_owner == actor_guid or not new_owner:
            raise ValueError("transfer to someone else")
        await _within_project(conn, conversation_id, new_owner)
        now = _now()
        held = await conn.fetch_value(
            "SELECT role FROM conversation_members WHERE conversation_id = ? AND user_guid = ?",
            (conversation_id, new_owner),
        )
        if held is None:
            await conn.execute(
                """INSERT INTO conversation_members
                    (conversation_id, user_guid, role, added_by, added_at)
                    VALUES (?, ?, 'contributor', ?, ?)""",
                (conversation_id, new_owner, actor_guid, now),
            )
        old_star = await conn.fetch_value(
            "SELECT is_favourite FROM conversations WHERE id = ?", (conversation_id,)
        )
        new_star = await conn.fetch_value(
            "SELECT is_favourite FROM conversation_members"
            " WHERE conversation_id = ? AND user_guid = ?",
            (conversation_id, new_owner),
        )
        # The old owner steps down first, so the one-owner index always holds.
        stepped_down = await conn.fetch_value(
            """UPDATE conversation_members SET role = 'contributor', is_favourite = ?
                WHERE conversation_id = ? AND user_guid = ? RETURNING user_guid""",
            (int(old_star or 0), conversation_id, actor_guid),
        )
        if stepped_down is None:  # an owner without a member row (made outside the app)
            await conn.execute(
                """INSERT INTO conversation_members
                    (conversation_id, user_guid, role, added_by, added_at, last_seen_at,
                     is_favourite)
                    VALUES (?, ?, 'contributor', ?, ?, ?, ?)""",
                (conversation_id, actor_guid, actor_guid, now, now, int(old_star or 0)),
            )
        await conn.execute(
            """UPDATE conversation_members SET role = 'owner'
                WHERE conversation_id = ? AND user_guid = ?""",
            (conversation_id, new_owner),
        )
        await conn.execute(
            "UPDATE conversations SET user_guid = ?, is_favourite = ? WHERE id = ?",
            (new_owner, int(new_star or 0), conversation_id),
        )
        await conn.execute(
            """DELETE FROM conversation_links
                WHERE source_conversation_id = ? OR target_conversation_id = ?""",
            (conversation_id, conversation_id),
        )


async def mark_seen(db: Executor, user_guid: str, conversation_id: int) -> None:
    """Record that the user opened a conversation they are a member of (clears "new")."""
    await db.execute(
        "UPDATE conversation_members SET last_seen_at = ? WHERE conversation_id = ? AND user_guid = ?",
        (_now(), conversation_id, user_guid),
    )


async def set_favourite(db: Executor, user_guid: str, conversation_id: int, value: bool) -> bool:
    """Star or unstar a conversation the user may read, for them alone. Returns False when
    they may not read it. The owner's star is also kept on the conversation row."""
    async with db.transaction() as conn:
        try:
            role = await require_access(conn, user_guid, conversation_id, Capability.READ)
        except NotOwned:
            return False
        await conn.execute(
            "UPDATE conversation_members SET is_favourite = ?"
            " WHERE conversation_id = ? AND user_guid = ?",
            (int(value), conversation_id, user_guid),
        )
        if role is Role.OWNER:
            await conn.execute(
                "UPDATE conversations SET is_favourite = ? WHERE id = ?",
                (int(value), conversation_id),
            )
    return True


async def shared_with(db: Executor, user_guid: str) -> list[Row]:
    """Active conversations shared with the user (not their own), newest first.

    Each row is the conversation's columns, with ``role`` (the user's), ``owner_guid``,
    ``is_new`` (1 until they open it), ``is_favourite`` (their own star) and
    ``message_count``.
    """
    return await db.fetch_all(
        """SELECT c.id, c.name, c.model_id, c.created_at, c.last_updated,
                c.conversation_type, c.total_tokens, c.user_guid AS owner_guid,
                cm.role, cm.is_favourite,
                CASE WHEN cm.last_seen_at IS NULL THEN 1 ELSE 0 END AS is_new,
                (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id = c.id AND m.is_rolled_up = 0) AS message_count
            FROM conversation_members cm
            JOIN conversations c ON c.id = cm.conversation_id
            WHERE cm.user_guid = ? AND cm.role <> 'owner' AND c.is_active = 1
            ORDER BY c.last_updated DESC""",
        (user_guid,),
    )


async def unseen_count(db: Executor, user_guid: str) -> int:
    """How many active conversations shared with the user they have not opened yet."""
    value = await db.fetch_value(
        """SELECT COUNT(*) FROM conversation_members cm
            JOIN conversations c ON c.id = cm.conversation_id
            WHERE cm.user_guid = ? AND cm.role <> 'owner' AND cm.last_seen_at IS NULL
            AND c.is_active = 1""",
        (user_guid,),
    )
    return int(value or 0)


# -- The turn lock ------------------------------------------------------------------------


async def acquire_turn(
    db: Executor,
    user_guid: str,
    conversation_id: int,
    stream_id: str,
    replica_id: str,
    *,
    stale_after: float = STALE_AFTER_SECONDS,
) -> Row | None:
    """Take the conversation's turn for ``user_guid``. Returns None when taken, or the
    holder's lease (``user_guid``, ``stream_id``, ``started_at``) when someone's turn is
    running. A lease not refreshed for ``stale_after`` seconds is taken over.

    Raises :class:`NotOwned` / :class:`NotPermitted` unless the user may send.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = (now - timedelta(seconds=stale_after)).isoformat(sep=" ")
    stamp = now.isoformat(sep=" ")
    async with db.transaction() as conn:
        await require_access(conn, user_guid, conversation_id, Capability.CONTRIBUTE)
        await conn.execute(
            "DELETE FROM conversation_turns WHERE conversation_id = ? AND heartbeat_at < ?",
            (conversation_id, cutoff),
        )
        taken = await conn.fetch_value(
            """INSERT INTO conversation_turns
                (conversation_id, user_guid, stream_id, replica_id, started_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (conversation_id) DO NOTHING
                RETURNING conversation_id""",
            (conversation_id, user_guid, stream_id, replica_id, stamp, stamp),
        )
        if taken is not None:
            return None
        return await conn.fetch_one(
            "SELECT user_guid, stream_id, started_at FROM conversation_turns"
            " WHERE conversation_id = ?",
            (conversation_id,),
        )


async def heartbeat_turn(db: Executor, conversation_id: int, stream_id: str) -> bool:
    """Refresh the lease of the turn ``stream_id``. False when it is no longer held
    (released, or taken over after it went stale).

    Deliberately unscoped: keyed by the turn's own stream id, which only the replica
    running the turn holds; no request names it.
    """
    held = await db.fetch_value(
        """UPDATE conversation_turns SET heartbeat_at = ?
            WHERE conversation_id = ? AND stream_id = ? RETURNING conversation_id""",
        (_now(), conversation_id, stream_id),
    )
    return held is not None


async def release_turn(db: Executor, conversation_id: int, stream_id: str) -> None:
    """Give back the turn ``stream_id`` holds (nothing when it holds none).

    Deliberately unscoped, as :func:`heartbeat_turn`: only the turn's own runner calls it.
    """
    await db.execute(
        "DELETE FROM conversation_turns WHERE conversation_id = ? AND stream_id = ?",
        (conversation_id, stream_id),
    )


async def current_turn(db: Executor, user_guid: str, conversation_id: int) -> Row | None:
    """The running turn's holder (``user_guid``, ``stream_id``, ``started_at``) in a
    conversation the user may read, or None."""
    return await db.fetch_one(
        f"""SELECT user_guid, stream_id, started_at FROM conversation_turns
            WHERE conversation_id = ? AND conversation_id IN ({READABLE_CONVERSATIONS})""",
        (conversation_id, user_guid),
    )
