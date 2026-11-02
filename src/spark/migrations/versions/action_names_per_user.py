"""Action names unique per user: ``UNIQUE(user_guid, name)`` on ``autonomous_actions``.

Revision ID: action_names_per_user
Revises: user_identities

0.2.0 made an action's name unique across the whole database, which on a
server would let one user's action name block (and reveal) another's. The
upgrade replaces that constraint with one per user. Existing names are already
unique, so no row changes.

PostgreSQL drops and adds the constraint. SQLite cannot drop a column's inline
``UNIQUE``, so the table is rebuilt (create, copy every column by name, drop,
rename) and its two indexes are created again; the migration connection does
not enforce foreign keys, so ``action_runs`` and ``action_tool_permissions``
keep pointing at the same ids.

The downgrade restores the global constraint. While two users share an action
name it refuses, changing nothing, rather than dropping or renaming anyone's
action: rename one of them first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "action_names_per_user"
down_revision = "user_identities"
branch_labels = None
depends_on = None

_TABLE = "autonomous_actions"
_PG_GLOBAL = "autonomous_actions_name_key"
_PG_PER_USER = "uq_autonomous_actions_user_name"

_COLUMNS = (
    "id",
    "name",
    "description",
    "action_prompt",
    "model_id",
    "schedule_type",
    "schedule_config",
    "context_mode",
    "max_failures",
    "failure_count",
    "is_enabled",
    "max_tokens",
    "created_at",
    "last_run_at",
    "next_run_at",
    "user_guid",
    "version",
    "locked_by",
    "locked_at",
    "updated_at",
)

# The 0.2.0 definition (SQLite), with the name constraint left to ``{unique}``.
_SQLITE_TABLE = """CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT {inline} NOT NULL,
            description TEXT,
            action_prompt TEXT NOT NULL,
            model_id TEXT NOT NULL,
            schedule_type TEXT NOT NULL DEFAULT 'one_off',
            schedule_config TEXT,
            context_mode TEXT NOT NULL DEFAULT 'fresh',
            max_failures INTEGER DEFAULT 3,
            failure_count INTEGER DEFAULT 0,
            is_enabled INTEGER DEFAULT 1,
            max_tokens INTEGER DEFAULT 8192,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_run_at TIMESTAMP,
            next_run_at TIMESTAMP,
            user_guid TEXT NOT NULL DEFAULT '',
            version INTEGER DEFAULT 1,
            locked_by TEXT,
            locked_at TIMESTAMP,
            updated_at TIMESTAMP{table_constraint}
        )"""

_SQLITE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_actions_enabled ON autonomous_actions(is_enabled)",
    "CREATE INDEX IF NOT EXISTS idx_actions_next_run ON autonomous_actions(next_run_at)",
)


def _sqlite() -> bool:
    return bool(op.get_bind().dialect.name == "sqlite")


def _rebuild_sqlite(*, per_user: bool) -> None:
    """Rebuild the table with the per-user or the global name constraint."""
    new = f"{_TABLE}_rebuild"
    op.execute(
        _SQLITE_TABLE.format(
            table=new,
            inline="" if per_user else "UNIQUE",
            table_constraint=",\n            UNIQUE(user_guid, name)" if per_user else "",
        )
    )
    columns = ", ".join(_COLUMNS)
    op.execute(f"INSERT INTO {new} ({columns}) SELECT {columns} FROM {_TABLE}")
    op.execute(f"DROP TABLE {_TABLE}")
    op.execute(f"ALTER TABLE {new} RENAME TO {_TABLE}")
    for statement in _SQLITE_INDEXES:
        op.execute(statement)


def upgrade() -> None:
    if _sqlite():
        _rebuild_sqlite(per_user=True)
        return
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_PG_GLOBAL}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_PG_PER_USER} UNIQUE (user_guid, name)")


def _refuse_shared_names() -> None:
    shared = (
        op.get_bind()
        .execute(sa.text(f"SELECT name FROM {_TABLE} GROUP BY name HAVING COUNT(*) > 1"))
        .fetchall()
    )
    if shared:
        raise RuntimeError(
            f"cannot downgrade {revision}: {len(shared)} action name(s) are used by more than "
            "one user, and before this revision action names were unique across all users; "
            "rename those actions first (nothing was changed)"
        )


def downgrade() -> None:
    _refuse_shared_names()
    if _sqlite():
        _rebuild_sqlite(per_user=False)
        return
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_PG_PER_USER}")
    op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_PG_GLOBAL} UNIQUE (name)")
