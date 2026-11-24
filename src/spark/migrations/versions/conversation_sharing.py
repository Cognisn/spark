"""Conversation sharing: ``conversation_members``, ``conversation_turns`` and per-participant
tool permissions.

Revision ID: conversation_sharing
Revises: message_reasoning

``conversation_members`` holds who may reach a conversation and as what: ``owner``,
``contributor`` or ``viewer``. ``conversations.user_guid`` still names the owner, and
the owner also has a member row, kept in step with it; the upgrade back-fills that row
for every existing conversation, already seen, so nothing shows as new. ``is_favourite``
is the member's own star; ``last_seen_at`` NULL marks a conversation shared with the
member and not opened yet.

``conversation_turns`` is the turn lock: at most one row per conversation, the lease
of the participant whose turn is running, refreshed by a heartbeat.

``conversation_tool_permissions`` widens its unique key from (conversation, tool) to
(conversation, user, tool), so one participant's "allow in this conversation" never
answers for another. PostgreSQL swaps the constraint; SQLite rebuilds the table (it
cannot drop an inline ``UNIQUE``). Existing rows keep their user, the owner's.

The downgrade drops the two tables and restores the narrower key. While two
participants hold a permission for the same tool in one conversation it refuses,
changing nothing, as the narrower key could not hold both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "conversation_sharing"
down_revision = "message_reasoning"
branch_labels = None
depends_on = None

_PERMS = "conversation_tool_permissions"
_PG_NARROW = "conversation_tool_permissions_conversation_id_tool_name_key"
_PG_WIDE = "uq_conversation_tool_permissions_user"

_PERM_COLUMNS = (
    "id",
    "conversation_id",
    "tool_name",
    "permission_state",
    "granted_at",
    "updated_at",
    "user_guid",
)

_SQLITE_PERMS = """CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            permission_state TEXT NOT NULL,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            UNIQUE({unique})
        )"""

_SQLITE_PERM_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tool_perms_conv ON conversation_tool_permissions(conversation_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_perms_tool ON conversation_tool_permissions(tool_name)",
)


def _sqlite() -> bool:
    return bool(op.get_bind().dialect.name == "sqlite")


def _rebuild_permissions(unique: str) -> None:
    new = f"{_PERMS}_rebuild"
    op.execute(_SQLITE_PERMS.format(table=new, unique=unique))
    columns = ", ".join(_PERM_COLUMNS)
    op.execute(f"INSERT INTO {new} ({columns}) SELECT {columns} FROM {_PERMS}")
    op.execute(f"DROP TABLE {_PERMS}")
    op.execute(f"ALTER TABLE {new} RENAME TO {_PERMS}")
    for statement in _SQLITE_PERM_INDEXES:
        op.execute(statement)


def upgrade() -> None:
    op.execute("""CREATE TABLE conversation_members (
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_guid TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner', 'contributor', 'viewer')),
            added_by TEXT NOT NULL,
            added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP,
            is_favourite INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (conversation_id, user_guid)
        )""")
    op.execute(
        "CREATE INDEX idx_conversation_members_user "
        "ON conversation_members(user_guid, conversation_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_conversation_members_owner "
        "ON conversation_members(conversation_id) WHERE role = 'owner'"
    )
    # Every existing conversation's owner, already seen (nothing is new to its owner).
    op.execute("""INSERT INTO conversation_members
            (conversation_id, user_guid, role, added_by, added_at, last_seen_at, is_favourite)
        SELECT id, user_guid, 'owner', user_guid, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
            COALESCE(is_favourite, 0)
        FROM conversations""")

    op.execute("""CREATE TABLE conversation_turns (
            conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            user_guid TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            replica_id TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            heartbeat_at TIMESTAMP NOT NULL
        )""")

    if _sqlite():
        _rebuild_permissions("conversation_id, user_guid, tool_name")
    else:
        op.execute(f"ALTER TABLE {_PERMS} DROP CONSTRAINT {_PG_NARROW}")
        op.execute(
            f"ALTER TABLE {_PERMS} ADD CONSTRAINT {_PG_WIDE} "
            "UNIQUE (conversation_id, user_guid, tool_name)"
        )


def _refuse_shared_permissions() -> None:
    shared = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT conversation_id, tool_name FROM {_PERMS} "
                "GROUP BY conversation_id, tool_name HAVING COUNT(*) > 1"
            )
        )
        .fetchall()
    )
    if shared:
        raise RuntimeError(
            f"cannot downgrade {revision}: {len(shared)} tool permission(s) are held by more "
            "than one participant of a conversation, and before this revision a conversation "
            "held one per tool; remove the participants' permissions first (nothing was changed)"
        )


def downgrade() -> None:
    _refuse_shared_permissions()
    if _sqlite():
        _rebuild_permissions("conversation_id, tool_name")
    else:
        op.execute(f"ALTER TABLE {_PERMS} DROP CONSTRAINT {_PG_WIDE}")
        op.execute(
            f"ALTER TABLE {_PERMS} ADD CONSTRAINT {_PG_NARROW} UNIQUE (conversation_id, tool_name)"
        )
    op.execute("DROP TABLE conversation_turns")
    op.execute("DROP INDEX uq_conversation_members_owner")
    op.execute("DROP INDEX idx_conversation_members_user")
    op.execute("DROP TABLE conversation_members")
