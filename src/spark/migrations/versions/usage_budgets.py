"""Usage budgets: per-call usage rows with a source and a UTC month, and monthly token budgets.

``usage_tracking`` gains the source of each model call (chat, compaction,
agent, debate, panel or action), the action it ran for, and its period: the
UTC calendar month (``YYYY-MM``) the call was recorded in, which is the budget
month (A14). Existing rows take the month of their timestamp and the source
``chat``.

``usage_role_budgets`` holds a monthly token budget per role, and
``usage_user_budgets`` a per-user override, where a NULL budget means the user
is explicitly unlimited.

Revision ID: usage_budgets
Revises: action_names_per_user
"""

from __future__ import annotations

from alembic import op

revision = "usage_budgets"
down_revision = "action_names_per_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_tracking ADD COLUMN source TEXT NOT NULL DEFAULT 'chat'")
    op.execute("ALTER TABLE usage_tracking ADD COLUMN action_id INTEGER")
    op.execute("ALTER TABLE usage_tracking ADD COLUMN period TEXT NOT NULL DEFAULT ''")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("UPDATE usage_tracking SET period = to_char(timestamp, 'YYYY-MM')")
    else:
        op.execute("UPDATE usage_tracking SET period = substr(timestamp, 1, 7)")
    op.execute("CREATE INDEX idx_usage_user_period ON usage_tracking(user_guid, period)")
    op.execute("""CREATE TABLE usage_role_budgets (
            role TEXT PRIMARY KEY,
            monthly_tokens BIGINT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    op.execute("""CREATE TABLE usage_user_budgets (
            user_guid TEXT PRIMARY KEY,
            monthly_tokens BIGINT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")


def downgrade() -> None:
    op.execute("DROP TABLE usage_user_budgets")
    op.execute("DROP TABLE usage_role_budgets")
    op.execute("DROP INDEX idx_usage_user_period")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN period")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN action_id")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN source")
