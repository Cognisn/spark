"""The projects revision: on the chain, the five-level limit at the database boundary, the
member and resource tables, ``conversations.project_id`` and the notices, with its upgrade
and downgrade on both engines."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest

from spark.db import migrate
from spark.mode import DatabaseSettings, Dialect

REVISION = "projects"
PREVIOUS = "artefact_titles"
TABLES = {"projects", "project_members", "project_resources", "project_notices"}


def test_revision_follows_artefact_titles() -> None:
    script = migrate._scripts().get_revision(REVISION)
    assert script is not None
    assert script.down_revision == PREVIOUS


def test_on_the_chain_to_head() -> None:
    chain = migrate._scripts().walk_revisions(base="base", head=migrate.head())
    assert REVISION in {r.revision for r in chain}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def _columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _project(conn: sqlite3.Connection, parent: int | None, depth: int) -> int:
    root = None
    if parent is not None:
        root = conn.execute("SELECT root_id FROM projects WHERE id = ?", (parent,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO projects (user_guid, parent_id, root_id, depth, name) VALUES (?, ?, ?, ?, ?)",
        ("ada", parent, root, depth, f"level {depth}"),
    )
    new = int(cur.lastrowid or 0)
    if parent is None:
        conn.execute("UPDATE projects SET root_id = ? WHERE id = ?", (new, new))
    return new


def test_upgrade_and_downgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "spark.db"
    settings = DatabaseSettings(Dialect.SQLITE, sqlite_path=path)
    migrate.upgrade(settings, target=PREVIOUS)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO conversations (name, user_guid) VALUES ('a', 'ada')")
    assert not TABLES & _tables(path)
    assert "project_id" not in _columns(path, "conversations")

    migrate.upgrade(settings, target=REVISION)
    assert TABLES <= _tables(path)
    assert "project_id" in _columns(path, "conversations")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        # Existing conversations are in no project.
        assert conn.execute("SELECT project_id FROM conversations").fetchall() == [(None,)]
        parent = None
        for depth in range(1, 6):
            parent = _project(conn, parent, depth)
        # The database refuses a sixth level, a top level that is not level 1, and a
        # sub-project at level 1 (the code refuses first, with a message).
        with pytest.raises(sqlite3.IntegrityError):
            _project(conn, parent, 6)
        with pytest.raises(sqlite3.IntegrityError):
            _project(conn, None, 2)
        with pytest.raises(sqlite3.IntegrityError):
            _project(conn, parent, 1)
        top = conn.execute("SELECT id FROM projects WHERE depth = 1").fetchone()[0]
        conn.execute(
            "INSERT INTO project_members (project_id, user_guid, role, scope, added_by)"
            " VALUES (?, 'cora', 'contributor', 'all_conversations', 'ada')",
            (top,),
        )
        for bad in (("owner", "project_only"), ("contributor", "everything")):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO project_members (project_id, user_guid, role, scope, added_by)"
                    " VALUES (?, 'vic', ?, ?, 'ada')",
                    (top, *bad),
                )
        conn.execute(
            "INSERT INTO user_files (user_guid, storage_key, filename, size_bytes)"
            " VALUES ('ada', 'projects/1/a.txt', 'a.txt', 3)"
        )
        conn.execute(
            "INSERT INTO project_resources (project_id, file_id, added_by) VALUES (?, 1, 'cora')",
            (top,),
        )
        with pytest.raises(sqlite3.IntegrityError):  # a file once per project
            conn.execute(
                "INSERT INTO project_resources (project_id, file_id, added_by)"
                " VALUES (?, 1, 'ada')",
                (top,),
            )
        # A deleted file is no longer a resource; a deleted project takes its tree.
        conn.execute("DELETE FROM user_files WHERE id = 1")
        assert conn.execute("SELECT COUNT(*) FROM project_resources").fetchone()[0] == 0
        conn.execute("DELETE FROM projects WHERE id = ?", (top,))
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM project_members").fetchone()[0] == 0
        conn.execute(
            "INSERT INTO project_notices (user_guid, kind, subject) VALUES ('cora', 'x', 'y')"
        )

    migrate.downgrade(settings, target=PREVIOUS)
    assert not TABLES & _tables(path)
    assert "project_id" not in _columns(path, "conversations")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM conversations").fetchall() == [("a",)]


@pytest.mark.postgres
def test_upgrade_and_downgrade_on_postgresql(pg_url: str) -> None:
    settings = DatabaseSettings(Dialect.POSTGRESQL, url=pg_url)

    def exists(table: str) -> bool:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"spark.{table}",)).fetchone()
        return bool(row and row[0])

    def has_project_column() -> bool:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'spark'"
                " AND table_name = 'conversations' AND column_name = 'project_id'"
            ).fetchone()
        return row is not None

    migrate.upgrade(settings, target=PREVIOUS)
    assert not any(exists(t) for t in TABLES)
    assert not has_project_column()

    migrate.upgrade(settings, target=REVISION)
    assert all(exists(t) for t in TABLES)
    assert has_project_column()
    with psycopg.connect(pg_url, autocommit=True) as conn:

        def project(parent: int | None, depth: int) -> int:
            root = None
            if parent is not None:
                root = conn.execute(
                    "SELECT root_id FROM spark.projects WHERE id = %s", (parent,)
                ).fetchone()[  # type: ignore[index]
                    0
                ]
            new = conn.execute(
                "INSERT INTO spark.projects (user_guid, parent_id, root_id, depth, name)"
                " VALUES ('ada', %s, %s, %s, 'p') RETURNING id",
                (parent, root, depth),
            ).fetchone()[  # type: ignore[index]
                0
            ]
            if parent is None:
                conn.execute("UPDATE spark.projects SET root_id = id WHERE id = %s", (new,))
            return int(new)

        parent = None
        for depth in range(1, 6):
            parent = project(parent, depth)
        with pytest.raises(psycopg.errors.CheckViolation):
            project(parent, 6)
        with pytest.raises(psycopg.errors.CheckViolation):
            project(None, 3)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO spark.project_members (project_id, user_guid, role, scope, added_by)"
                " SELECT id, 'vic', 'viewer', 'some', 'ada' FROM spark.projects WHERE depth = 1"
            )

    migrate.downgrade(settings, target=PREVIOUS)
    assert not any(exists(t) for t in TABLES)
    assert not has_project_column()
