"""Project cards: ``project_cards``, the pending actions of the project tools (PC17, PC18).

Revision ID: project_cards
Revises: projects

A card is what a project tool shows instead of acting: a suggested project (Create, Edit,
Dismiss), a before-and-after update of a project (Confirm, Cancel) or a move of the
conversation into a project with its visibility report (Confirm, Cancel). Each row is one
card, kept on the server under an unguessable ``id``, bound to the turn's sender
(``user_guid``) and its conversation, until ``expires_at``; ``status`` moves once from
``pending`` to ``accepted``, ``dismissed`` or ``cancelled``. ``payload`` is the card's
JSON (what it proposes and, for an update or a move, what it showed).

A conversation holds at most one suggestion card, whatever became of it (the unique
partial index): SPARK suggests a project once per conversation and never again after
Dismiss. A deleted conversation takes its cards. Times are ISO 8601 text in UTC, so both
engines compare them the same way.

The downgrade drops the table.
"""

from __future__ import annotations

from alembic import op

revision = "project_cards"
down_revision = "projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE project_cards (
            id TEXT PRIMARY KEY,
            user_guid TEXT NOT NULL,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('suggestion', 'update', 'move')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'dismissed', 'cancelled')),
            project_id INTEGER,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT
        )""")
    op.execute("CREATE INDEX idx_project_cards_conversation ON project_cards(conversation_id)")
    op.execute("""CREATE UNIQUE INDEX ux_project_cards_suggestion
            ON project_cards(conversation_id) WHERE kind = 'suggestion'""")


def downgrade() -> None:
    op.execute("DROP INDEX ux_project_cards_suggestion")
    op.execute("DROP INDEX idx_project_cards_conversation")
    op.execute("DROP TABLE project_cards")
