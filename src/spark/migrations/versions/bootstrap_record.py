"""The instance's bootstrap record: ``bootstrap``, one row once ``spark bootstrap`` has finished.

Revision ID: bootstrap_record
Revises: overlay_connection_scope

``spark bootstrap`` (server mode) runs once after ``spark migrate``: it creates the
restricted database logins, the instance identity (warden's signing key, the TOTP
key and the CSRF key) and the first administrator's invitation, then writes this
row. Web and worker wait for it before they start, and a later run that finds it
sends no second invitation. The table holds at most one row (``id`` is always 1)
and no secret: when it completed, the warden user it invited or found, and the
SPARK version that wrote it. Personal mode never writes it.
"""

from __future__ import annotations

from alembic import op

revision = "bootstrap_record"
down_revision = "overlay_connection_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE bootstrap (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            completed_at TIMESTAMP NOT NULL,
            administrator_id TEXT,
            spark_version TEXT NOT NULL
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE bootstrap")
