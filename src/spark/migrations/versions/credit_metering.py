"""Credit metering: credits on every usage row, and A14's token budgets removed (M6 to M10).

Revision ID: credit_metering
Revises: connections

``usage_tracking`` gains, per model call:

* ``credits_micro``: the credits the call cost, in whole micro-credits (one credit
  is 1,000,000; :mod:`spark.usage.credits`), zero for rows from before metering;
* ``credit_inputs``: the rates the call was charged at, as JSON (metered or not,
  the model's tier and multiplier, the base rate and the four token weights), so a
  later change of a connection's rates never rewrites what was charged.

Monthly allowances per metered connection (``connection_assignments``) replace
A14's role and per-user token budgets, so ``usage_role_budgets`` and
``usage_user_budgets`` are dropped. 0.3.0 is unreleased, so nothing is migrated
from them. The downgrade recreates both tables empty and drops the two columns.
"""

from __future__ import annotations

from alembic import op

revision = "credit_metering"
down_revision = "connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_tracking ADD COLUMN credits_micro BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE usage_tracking ADD COLUMN credit_inputs TEXT")
    op.execute("DROP TABLE usage_user_budgets")
    op.execute("DROP TABLE usage_role_budgets")


def downgrade() -> None:
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
    op.execute("ALTER TABLE usage_tracking DROP COLUMN credit_inputs")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN credits_micro")
