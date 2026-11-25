"""Projects: ``projects``, ``project_members``, ``project_resources``, ``project_notices`` and
``conversations.project_id``.

Revision ID: projects
Revises: conversation_sharing

A project belongs to the user ``projects.user_guid`` names. A sub-project names its
``parent_id``; every project of one tree carries the id of its top-level project in
``root_id`` and its level in ``depth`` (the top level is 1), so the access decision
reaches a tree's members in one join. The database refuses a level outside 1 to 5 and a
level that does not match having a parent; the code refuses first, with a message.

``project_members`` holds who a top-level project is shared with: a ``contributor`` or a
``viewer`` (Read-only), with the scope ``project_only`` or ``all_conversations``.
``project_resources`` links a project to files in its owner's file area, with who added
each; a file deleted from the area is no longer a resource. ``project_notices`` holds what
a person is told about a project (its deletion took their conversations, they became its
owner), until they have seen it.

``conversations.project_id`` places a conversation in a project (NULL: no project). It has
no foreign key, so the downgrade can drop it on SQLite; the code keeps it in step.

The downgrade drops all of it; conversations simply leave their projects.
"""

from __future__ import annotations

from alembic import op

revision = "projects"
down_revision = "conversation_sharing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    auto = (
        "SERIAL PRIMARY KEY"
        if op.get_bind().dialect.name == "postgresql"
        else "INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    op.execute(f"""CREATE TABLE projects (
            id {auto},
            user_guid TEXT NOT NULL,
            parent_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            root_id INTEGER,
            depth INTEGER NOT NULL CHECK (depth BETWEEN 1 AND 5),
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK ((parent_id IS NULL AND depth = 1) OR (parent_id IS NOT NULL AND depth > 1))
        )""")
    op.execute("CREATE INDEX idx_projects_user ON projects(user_guid)")
    op.execute("CREATE INDEX idx_projects_parent ON projects(parent_id)")
    op.execute("CREATE INDEX idx_projects_root ON projects(root_id)")

    op.execute("""CREATE TABLE project_members (
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_guid TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('contributor', 'viewer')),
            scope TEXT NOT NULL CHECK (scope IN ('project_only', 'all_conversations')),
            added_by TEXT NOT NULL,
            added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, user_guid)
        )""")
    op.execute("CREATE INDEX idx_project_members_user ON project_members(user_guid, project_id)")

    op.execute(f"""CREATE TABLE project_resources (
            id {auto},
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
            added_by TEXT NOT NULL,
            added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (project_id, file_id)
        )""")
    op.execute("CREATE INDEX idx_project_resources_file ON project_resources(file_id)")

    op.execute(f"""CREATE TABLE project_notices (
            id {auto},
            user_guid TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT NOT NULL,
            project_id INTEGER,
            actor_guid TEXT,
            count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            seen_at TIMESTAMP
        )""")
    op.execute("CREATE INDEX idx_project_notices_user ON project_notices(user_guid, seen_at)")

    op.execute("ALTER TABLE conversations ADD COLUMN project_id INTEGER")
    op.execute("CREATE INDEX idx_conversations_project ON conversations(project_id)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_conversations_project")
    op.execute("ALTER TABLE conversations DROP COLUMN project_id")
    op.execute("DROP INDEX idx_project_notices_user")
    op.execute("DROP TABLE project_notices")
    op.execute("DROP INDEX idx_project_resources_file")
    op.execute("DROP TABLE project_resources")
    op.execute("DROP INDEX idx_project_members_user")
    op.execute("DROP TABLE project_members")
    op.execute("DROP INDEX idx_projects_root")
    op.execute("DROP INDEX idx_projects_parent")
    op.execute("DROP INDEX idx_projects_user")
    op.execute("DROP TABLE projects")
