"""list_my_files and read_my_file in a project conversation (PC7, PC11): the conversation's
inherited resources are listed by level and read by id from the project owner's area,
by whoever sends the turn; another project's resource answers exactly as a file that does
not exist. Server mode on SQLite and (marked postgres) PostgreSQL; personal mode reads
the personal area.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark import files as files_pkg
from spark.database import conversations
from spark.database import project_members as members_db
from spark.database import projects as projects_db
from spark.mode import Mode
from spark.projects.access import ProjectRole, ShareScope
from spark.tools import my_files

ADA = "user-ada-example"
CORA = "user-cora-example"
PAT = "user-pat-example"
SAM = "user-sam-example"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


@pytest.fixture
def server_files(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "volume"
    files_pkg.configure(
        _Settings({"files.root": str(root)}), data_dir=tmp_path, runtime_mode=Mode.SERVER
    )
    yield root
    files_pkg.reset()


def upload(bridge: Any, project: int, name: str, text: str) -> int:
    """A resource uploaded into the project owner's area (as the projects route does)."""
    area = files_pkg.file_area(bridge)
    file_id = area.save(
        ADA,
        f"projects/{project}/{name}",
        io.BytesIO(text.encode()),
        filename=name,
        mime_type="text/plain",
        source="project",
    )
    bridge.call(projects_db.add_resource, ADA, project, file_id)
    return int(file_id)


def config(bridge: Any, user: str, cid: int) -> dict[str, Any]:
    return {"_user_guid": user, "_kg_db": bridge, "_kg_conversation_id": cid, "_kg_user_guid": user}


@pytest.fixture
def world(bridge: Any, server_files: Path) -> dict[str, int]:
    plan = bridge.call(projects_db.create_project, ADA, "Plan")
    detail = bridge.call(projects_db.create_project, ADA, "Detail", parent_id=plan)
    other = bridge.call(projects_db.create_project, ADA, "Other")
    bridge.call(
        members_db.share, ADA, plan, CORA, ProjectRole.CONTRIBUTOR, ShareScope.ALL_CONVERSATIONS
    )
    bridge.call(members_db.share, ADA, plan, PAT, ProjectRole.CONTRIBUTOR, ShareScope.PROJECT_ONLY)
    cid = bridge.call(conversations.create_conversation, "c", "model-a", CORA)
    bridge.call(projects_db.place_conversation, CORA, cid, detail)
    return {
        "plan": plan,
        "detail": detail,
        "other": other,
        "cid": cid,
        "top": upload(bridge, plan, "top.txt", "top secret plan"),
        "low": upload(bridge, detail, "low.txt", "detail notes"),
        "stray": upload(bridge, other, "stray.txt", "other project"),
    }


class TestServerMode:
    def test_a_member_reads_an_inherited_resource(self, bridge: Any, world: dict[str, int]) -> None:
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["top"]}, config(bridge, CORA, world["cid"])
        )
        assert not error and "top secret plan" in text

    def test_the_owner_reads_it_in_a_members_conversation(
        self, bridge: Any, world: dict[str, int]
    ) -> None:
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["low"]}, config(bridge, ADA, world["cid"])
        )
        assert not error and "detail notes" in text

    def test_another_projects_resource_is_not_found(
        self, bridge: Any, world: dict[str, int]
    ) -> None:
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["stray"]}, config(bridge, CORA, world["cid"])
        )
        assert error and "was not found" in text and "other project" not in text

    def test_without_the_conversation_nothing_is_inherited(
        self, bridge: Any, world: dict[str, int]
    ) -> None:
        # Pat (project only) may not read Cora's conversation: no resources through it.
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["top"]}, config(bridge, PAT, world["cid"])
        )
        assert error and "top secret" not in text
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["top"]}, config(bridge, SAM, world["cid"])
        )
        assert error and "top secret" not in text

    def test_listing_shows_each_level(self, bridge: Any, world: dict[str, int]) -> None:
        text, error = my_files.execute("list_my_files", {}, config(bridge, CORA, world["cid"]))
        assert not error
        assert "Project resources" in text
        assert f'file {world["top"]}: top.txt (text/plain, 15 bytes), from level 1 ("Plan")' in text
        assert f'file {world["low"]}: low.txt (text/plain, 12 bytes), from level 2 ("Detail")' in (
            text
        )
        assert f"file {world['stray']}" not in text

    def test_a_removed_member_reads_nothing(self, bridge: Any, world: dict[str, int]) -> None:
        # Cora started the conversation, which stays hers and stays in the project; the
        # project's resources do not.
        bridge.call(members_db.remove, ADA, world["plan"], CORA)
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["top"]}, config(bridge, CORA, world["cid"])
        )
        assert error and "top secret" not in text
        listed, _ = my_files.execute("list_my_files", {}, config(bridge, CORA, world["cid"]))
        assert "Project resources" not in listed
        # The owner still reads them there.
        text, error = my_files.execute(
            "read_my_file", {"file_id": world["low"]}, config(bridge, ADA, world["cid"])
        )
        assert not error and "detail notes" in text


@pytest.fixture
def personal_files(tmp_path: Path) -> Iterator[Path]:
    files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
    yield tmp_path / "files"
    files_pkg.reset()


def test_personal_mode_reads_the_personal_area(sync_db: Any, personal_files: Path) -> None:
    plan = sync_db.call(projects_db.create_project, ADA, "Plan")
    file_id = upload(sync_db, plan, "brief.txt", "personal brief")
    assert (personal_files).exists()
    cid = sync_db.call(conversations.create_conversation, "c", "model-a", ADA)
    sync_db.call(projects_db.place_conversation, ADA, cid, plan)
    text, error = my_files.execute("read_my_file", {"file_id": file_id}, config(sync_db, ADA, cid))
    assert not error and "personal brief" in text
    listed, _ = my_files.execute("list_my_files", {}, config(sync_db, ADA, cid))
    assert "Project resources" in listed and f"file {file_id}" in listed
