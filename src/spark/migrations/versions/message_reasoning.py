"""A message's reasoning: ``message_reasoning``, one row per assistant message that has any.

Revision ID: message_reasoning
Revises: bootstrap_record

A model's reasoning (Claude's thinking, Amazon Nova's ``<thinking>`` text) is shown
beside its reply, never as part of it. It is kept in its own table, not in a column of
``messages``, so no read of a message (the model's history, an export, compaction, the
vector index) can carry it by accident. A row belongs to one message and is removed with
it; its text is only what the user could read, never a signature.
"""

from __future__ import annotations

from alembic import op

revision = "message_reasoning"
down_revision = "bootstrap_record"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE message_reasoning (
            message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            content TEXT NOT NULL
        )""")
    op.execute(
        "CREATE INDEX idx_message_reasoning_conversation ON message_reasoning(conversation_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_message_reasoning_conversation")
    op.execute("DROP TABLE message_reasoning")
