"""File areas: per-user file metadata and the usage counter quotas are checked against.

Revision ID: file_areas
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "file_areas"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    auto = (
        "SERIAL PRIMARY KEY"
        if op.get_bind().dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    op.execute(f"""CREATE TABLE user_files (
            id {auto},
            user_guid TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            filename TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            mime_type TEXT,
            conversation_id INTEGER,
            source TEXT NOT NULL DEFAULT 'upload',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_guid, storage_key)
        )""")
    op.execute("CREATE INDEX idx_user_files_conversation ON user_files(conversation_id)")
    op.execute("""CREATE TABLE user_file_usage (
            user_guid TEXT PRIMARY KEY,
            bytes_used BIGINT NOT NULL DEFAULT 0
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE user_file_usage")
    op.execute("DROP INDEX idx_user_files_conversation")
    op.execute("DROP TABLE user_files")
