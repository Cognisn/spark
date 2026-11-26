"""Artefact titles and kinds on user files (PC5).

Revision ID: artefact_titles
Revises: conversation_sharing

Every file in a user's area gets a ``title`` (what the artefacts panel shows) and a
``kind``: ``document`` (made by a create tool), ``chart``, ``diagram``, ``preview`` (a
document fetched for the preview pane) or ``attachment`` (a file someone provided).
Existing files are back-filled: the title is the file name, and the kind follows the
source (``upload`` and ``import`` are attachments, ``preview`` is a preview, anything
SPARK made is a document). The values are checked by the repository
(:mod:`spark.database.files`), not by a constraint.

The downgrade drops both columns and keeps every file.
"""

from __future__ import annotations

from alembic import op

revision = "artefact_titles"
down_revision = "conversation_sharing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_files ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE user_files ADD COLUMN kind TEXT NOT NULL DEFAULT 'document'")
    op.execute("""UPDATE user_files SET title = filename,
            kind = CASE source
                WHEN 'upload' THEN 'attachment'
                WHEN 'import' THEN 'attachment'
                WHEN 'preview' THEN 'preview'
                ELSE 'document' END""")


def downgrade() -> None:
    op.execute("ALTER TABLE user_files DROP COLUMN kind")
    op.execute("ALTER TABLE user_files DROP COLUMN title")
