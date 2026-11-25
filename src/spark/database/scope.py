"""User scoping shared by the repositories, and the one access decision for conversations.

Every row a user reaches through a parent id (a conversation's messages, an
action's runs, a debate turn's exhibits) is reached only by a user the parent
allows. Repositories add one of the subqueries below to such a query, so a
caller holding an id they may not use gets nothing back and changes nothing.

**Conversations** are shared (server mode). The owner is the user named by
``conversations.user_guid``; other people reach a conversation through their
``conversation_members`` row, as a ``contributor`` or a ``viewer``. What a role may
do is one table, :data:`ALLOWS`:

* ``READ``: see the conversation, its history, attachments, tool activity, export;
* ``CONTRIBUTE``: send in it (the turn and everything it writes);
* ``MANAGE``: share, change roles, remove, transfer, rename, delete, model and settings.

:func:`conversation_role` and :func:`require_access` decide it for one id; the SQL
fragments decide it inside a query, each taking one parameter, the caller's guid:

* ``READABLE_CONVERSATIONS``: the owner's and every member's;
* ``CONTRIBUTABLE_CONVERSATIONS``: the owner's and contributors';
* ``OWNED_CONVERSATIONS``: the owner's only, for what stays private to the owner
  (linked conversations, debates and panels, knowledge graph builds, actions, a
  user's bundle).

``OWNED_ACTIONS`` takes the caller's guid as well.

**Conversations in a project** (PC9) are also reached through the project. A member of
the project's tree (``project_members`` on its top-level project) whose scope is
``all_conversations`` reaches every conversation of the tree, whoever started it, at
their project role (``contributor``, or ``viewer`` for Read-only); with ``project_only``
they reach none of them through the project. The project's owner reaches every
conversation of their tree as :data:`PROJECT_OWNER_ROLE`. A person with an individual
share as well holds the better of the two roles. Managing a conversation stays with
whoever started it: nothing reached through a project is ``MANAGE``.
"""

from __future__ import annotations

from enum import StrEnum

from spark.db.engine import Executor

__all__ = [
    "ALLOWS",
    "CONTRIBUTABLE_CONVERSATIONS",
    "OWNED_ACTIONS",
    "OWNED_CONVERSATIONS",
    "PROJECT_OWNER_ROLE",
    "READABLE_CONVERSATIONS",
    "Capability",
    "NotOwned",
    "NotPermitted",
    "Role",
    "allows",
    "conversation_role",
    "owns_action",
    "require_access",
]


class Role(StrEnum):
    """A person's place in a conversation."""

    OWNER = "owner"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class Capability(StrEnum):
    """What a request needs to do in a conversation."""

    READ = "read"
    CONTRIBUTE = "contribute"
    MANAGE = "manage"


ALLOWS: dict[Role, frozenset[Capability]] = {
    Role.OWNER: frozenset({Capability.READ, Capability.CONTRIBUTE, Capability.MANAGE}),
    Role.CONTRIBUTOR: frozenset({Capability.READ, Capability.CONTRIBUTE}),
    Role.VIEWER: frozenset({Capability.READ}),
}


def allows(role: Role | None, need: Capability) -> bool:
    """Whether ``role`` (None: not a member) may do ``need``."""
    return role is not None and need in ALLOWS[role]


# The role a project's owner holds in a conversation someone else started in their
# project (question 89; contributor: they read and send, they do not manage).
PROJECT_OWNER_ROLE: Role = Role.CONTRIBUTOR

_RANK = {Role.VIEWER: 1, Role.CONTRIBUTOR: 2, Role.OWNER: 3}


def _members_with(roles: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{r}'" for r in roles)
    project_roles = ", ".join(f"'{r}'" for r in roles if r != Role.OWNER.value)
    owner_reaches = PROJECT_OWNER_ROLE.value in roles
    return (
        "WITH caller AS (SELECT CAST(? AS TEXT) AS guid) "
        "SELECT c.id FROM conversations c JOIN caller ON c.user_guid = caller.guid "
        "UNION SELECT cm.conversation_id FROM conversation_members cm "
        f"JOIN caller ON cm.user_guid = caller.guid WHERE cm.role IN ({listed}) "
        # Through a project: every conversation of the tree for its all-conversations
        # members at their role, and for the project's owner.
        "UNION SELECT pc.id FROM conversations pc "
        "JOIN projects pp ON pp.id = pc.project_id "
        "JOIN project_members pm ON pm.project_id = pp.root_id "
        "JOIN caller ON pm.user_guid = caller.guid "
        f"WHERE pm.scope = 'all_conversations' AND pm.role IN ({project_roles})"
        + (
            " UNION SELECT oc.id FROM conversations oc "
            "JOIN projects op ON op.id = oc.project_id "
            "JOIN caller ON op.user_guid = caller.guid"
            if owner_reaches
            else ""
        )
    )


OWNED_CONVERSATIONS = "SELECT id FROM conversations WHERE user_guid = ?"
READABLE_CONVERSATIONS = _members_with(("owner", "contributor", "viewer"))
CONTRIBUTABLE_CONVERSATIONS = _members_with(("owner", "contributor"))
OWNED_ACTIONS = "SELECT id FROM autonomous_actions WHERE user_guid = ?"


class NotOwned(LookupError):
    """The parent does not exist for this user (it is missing, or they are not a member)."""


class NotPermitted(NotOwned):
    """The user is a member of the conversation, but their role does not allow this.

    A member already knows the conversation exists, so endpoints answer 403, not 404.
    """


async def conversation_role(db: Executor, user_guid: str, conversation_id: int) -> Role | None:
    """``user_guid``'s role in ``conversation_id``, or None (no such conversation, or not
    a member). The owner is whoever ``conversations.user_guid`` names."""
    if not user_guid:
        return None
    row = await db.fetch_one(
        """SELECT c.user_guid AS owner_guid,
                (SELECT cm.role FROM conversation_members cm
                 WHERE cm.conversation_id = c.id AND cm.user_guid = ?) AS member_role,
                (SELECT p.user_guid FROM projects p WHERE p.id = c.project_id)
                    AS project_owner,
                (SELECT pm.role FROM projects p
                 JOIN project_members pm ON pm.project_id = p.root_id
                 WHERE p.id = c.project_id AND pm.user_guid = ?
                 AND pm.scope = 'all_conversations') AS project_role
            FROM conversations c WHERE c.id = ?""",
        (user_guid, user_guid, conversation_id),
    )
    if row is None:
        return None
    if row["owner_guid"] == user_guid:
        return Role.OWNER
    held: list[Role] = []
    for value in (row["member_role"], row["project_role"]):
        if value in (Role.CONTRIBUTOR.value, Role.VIEWER.value):
            held.append(Role(value))
    if row["project_owner"] is not None and row["project_owner"] == user_guid:
        held.append(PROJECT_OWNER_ROLE)
    return max(held, key=_RANK.__getitem__, default=None)


async def require_access(
    db: Executor, user_guid: str, conversation_id: int, need: Capability
) -> Role:
    """``user_guid``'s role in ``conversation_id`` when it allows ``need``.

    Raises :class:`NotOwned` when the user is not a member (or there is no such
    conversation) and :class:`NotPermitted` when they are, without the capability.
    """
    role = await conversation_role(db, user_guid, conversation_id)
    if role is None:
        raise NotOwned(f"conversation {conversation_id} not found")
    if not allows(role, need):
        raise NotPermitted(f"conversation {conversation_id}: {role.value} may not {need.value}")
    return role


async def owns_action(db: Executor, user_guid: str, action_id: int) -> bool:
    """Whether ``action_id`` is one of ``user_guid``'s actions."""
    found = await db.fetch_value(
        "SELECT 1 FROM autonomous_actions WHERE id = ? AND user_guid = ?",
        (action_id, user_guid),
    )
    return found is not None
