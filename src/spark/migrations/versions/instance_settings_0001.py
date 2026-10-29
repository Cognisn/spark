"""Instance settings and user preferences (feat/0.3.0-instance-settings).

Administrator-controlled, non-secret settings (``instance_settings``) and per-user
preferences (``user_preferences``). Values are JSON text. No secret is ever stored
here: secrets live in konfig's secrets store.

Revision ID: instance_settings_0001
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "instance_settings_0001"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE instance_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT ''
        )""")
    op.execute("""CREATE TABLE user_preferences (
            user_guid TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_guid, key)
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE user_preferences")
    op.execute("DROP TABLE instance_settings")
