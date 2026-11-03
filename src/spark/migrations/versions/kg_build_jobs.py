"""Knowledge graph build jobs: the queue the worker builds from in server mode.

Revision ID: kg_build_jobs
Revises: action_names_per_user

A build the knowledge endpoint enqueues is one row, owned by ``user_guid``. Its
``status`` moves from ``queued`` to ``running`` when a worker claims it, then to
``complete``, ``failed`` or ``cancelled``; a failed job goes back to ``queued``
when its owner asks for a retry. ``progress`` and ``result`` hold JSON text.
Times are ISO 8601 UTC text, written by SPARK.

The partial unique index allows one active (queued or running) job per user
and scope, so two web replicas cannot both start a build of the same graph.

Personal mode builds in-process and leaves the table empty; it is created on
both engines so one chain serves both. The downgrade drops the table and its
indexes.
"""

from __future__ import annotations

from alembic import op

revision = "kg_build_jobs"
down_revision = "action_names_per_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    auto = (
        "BIGSERIAL PRIMARY KEY"
        if op.get_bind().dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    op.execute(f"""CREATE TABLE kg_build_jobs (
            id {auto},
            user_guid TEXT NOT NULL,
            scope TEXT NOT NULL,
            model_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'complete', 'failed', 'cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            claimed_by TEXT,
            progress TEXT,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )""")
    op.execute("""CREATE UNIQUE INDEX uq_kg_build_jobs_active
            ON kg_build_jobs (user_guid, scope)
            WHERE status IN ('queued', 'running')""")
    op.execute("CREATE INDEX idx_kg_build_jobs_status ON kg_build_jobs (status, id)")
    op.execute("CREATE INDEX idx_kg_build_jobs_owner ON kg_build_jobs (user_guid, scope, id)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_kg_build_jobs_owner")
    op.execute("DROP INDEX idx_kg_build_jobs_status")
    op.execute("DROP INDEX uq_kg_build_jobs_active")
    op.execute("DROP TABLE kg_build_jobs")
