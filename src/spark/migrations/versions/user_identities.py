"""User identities: each SPARK user guid mapped onto one warden user (server mode identity).

Every existing ``user_guid`` column is left as it is; this table only records
which warden user a guid belongs to, so data made before warden (the interim
single-user guid) stays attributable once that guid is linked to a warden user.

Revision ID: user_identities
Revises: file_areas
"""

from __future__ import annotations

from alembic import op

revision = "user_identities"
down_revision = "file_areas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE user_identities (
            user_guid TEXT PRIMARY KEY,
            warden_user_id TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE user_identities")
