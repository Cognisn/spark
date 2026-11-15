"""Named model connections, their models, SPARK's catalogue overlay, metering and assignments.

Revision ID: connections
Revises: kg_build_jobs

The schema SPARK's conduit adoption builds on (M3 to M10, M15):

* ``connections``: one row per named connection. ``settings`` is the conduit
  provider settings for its kind as JSON, every credential a ``secret://``
  reference (never a value, A9); ``residency`` is the policy as JSON. Server
  mode's connection store; personal mode keeps its connections in ``config.yaml``.
* ``connection_models``: the models a connection offers, each enabled or not and
  of a credit tier (``lower``, ``standard`` or ``higher``; ``standard`` by default).
* ``catalogue_overlays``: SPARK's overlay entries on conduit's model catalogue,
  keyed by catalogue provider and model id (a model conduit does not ship, such
  as one pulled into Ollama, or a correction of one it does).
* ``connection_metering``: whether a connection is metered, its base rate (weighted
  tokens per credit on a standard model), the higher and lower tier multipliers,
  and the weights of input, output, cache read and cache write tokens (1, 5, 0.1
  and 1.25 by default).
* ``connection_assignments``: a connection assigned to a role or to one user,
  with a monthly credit allowance (NULL when the connection is unmetered).

``usage_tracking`` gains the connection each call ran on and the cache read and
write tokens apart from ``input_tokens``, which from now on counts uncached
input only. Existing rows keep their totals: their cache counts are zero and
their ``input_tokens`` already included the cache.

The child tables reference ``connections`` with ``ON DELETE CASCADE``; the
repository also deletes them itself, so both engines behave alike. Times are ISO
8601 UTC text, written by SPARK. The downgrade drops everything it added.
"""

from __future__ import annotations

from alembic import op

revision = "connections"
down_revision = "kg_build_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    auto = "BIGSERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    op.execute("""CREATE TABLE connections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            settings TEXT NOT NULL,
            residency TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
    op.execute("""CREATE TABLE connection_models (
            connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
            model_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            tier TEXT NOT NULL DEFAULT 'standard'
                CHECK (tier IN ('lower', 'standard', 'higher')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (connection_id, model_id)
        )""")
    op.execute("""CREATE TABLE catalogue_overlays (
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'embedding')),
            context_window INTEGER NOT NULL CHECK (context_window > 0),
            output_limit INTEGER,
            embedding_dimensions INTEGER,
            tools INTEGER NOT NULL DEFAULT 0,
            vision INTEGER NOT NULL DEFAULT 0,
            prompt_caching INTEGER NOT NULL DEFAULT 0,
            structured_output INTEGER NOT NULL DEFAULT 0,
            residency_scope TEXT NOT NULL
                CHECK (residency_scope IN ('local', 'in_region', 'cross_region', 'global')),
            regions TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, model_id)
        )""")
    op.execute("""CREATE TABLE connection_metering (
            connection_id TEXT PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
            metered INTEGER NOT NULL DEFAULT 0,
            base_rate_tokens BIGINT NOT NULL DEFAULT 1000 CHECK (base_rate_tokens > 0),
            higher_multiplier DOUBLE PRECISION NOT NULL DEFAULT 2 CHECK (higher_multiplier > 0),
            lower_multiplier DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (lower_multiplier > 0),
            input_weight DOUBLE PRECISION NOT NULL DEFAULT 1 CHECK (input_weight >= 0),
            output_weight DOUBLE PRECISION NOT NULL DEFAULT 5 CHECK (output_weight >= 0),
            cache_read_weight DOUBLE PRECISION NOT NULL DEFAULT 0.1
                CHECK (cache_read_weight >= 0),
            cache_write_weight DOUBLE PRECISION NOT NULL DEFAULT 1.25
                CHECK (cache_write_weight >= 0),
            updated_at TEXT NOT NULL
        )""")
    op.execute(f"""CREATE TABLE connection_assignments (
            id {auto},
            connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
            role TEXT,
            user_guid TEXT,
            monthly_credits BIGINT CHECK (monthly_credits IS NULL OR monthly_credits >= 0),
            updated_at TEXT NOT NULL,
            CHECK ((role IS NULL AND user_guid IS NOT NULL)
                OR (role IS NOT NULL AND user_guid IS NULL))
        )""")
    op.execute("""CREATE UNIQUE INDEX uq_connection_assignments_role
            ON connection_assignments (connection_id, role) WHERE role IS NOT NULL""")
    op.execute("""CREATE UNIQUE INDEX uq_connection_assignments_user
            ON connection_assignments (connection_id, user_guid) WHERE user_guid IS NOT NULL""")
    op.execute("CREATE INDEX idx_connection_assignments_user ON connection_assignments (user_guid)")
    op.execute("ALTER TABLE usage_tracking ADD COLUMN connection_id TEXT")
    op.execute("ALTER TABLE usage_tracking ADD COLUMN cache_read_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE usage_tracking ADD COLUMN cache_write_tokens BIGINT NOT NULL DEFAULT 0")
    op.execute(
        "CREATE INDEX idx_usage_connection_period "
        "ON usage_tracking (connection_id, user_guid, period)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_usage_connection_period")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN cache_write_tokens")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN cache_read_tokens")
    op.execute("ALTER TABLE usage_tracking DROP COLUMN connection_id")
    op.execute("DROP INDEX idx_connection_assignments_user")
    op.execute("DROP INDEX uq_connection_assignments_user")
    op.execute("DROP INDEX uq_connection_assignments_role")
    op.execute("DROP TABLE connection_assignments")
    op.execute("DROP TABLE connection_metering")
    op.execute("DROP TABLE catalogue_overlays")
    op.execute("DROP TABLE connection_models")
    op.execute("DROP TABLE connections")
