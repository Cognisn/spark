"""Baseline: the SPARK 0.2.0 schema.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

from spark.migrations.baseline import baseline_statements, table_names

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    for sql in baseline_statements(dialect):
        op.execute(sql)


def downgrade() -> None:
    for table in reversed(table_names()):
        op.execute(f"DROP TABLE {table}")
