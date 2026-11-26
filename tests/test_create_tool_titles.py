"""Every create tool takes a required title, records it and returns the new file's id (PC5).

create_word, create_excel, create_powerpoint and create_pdf (create_chart and
create_diagram are covered with the charts): the title is required in the tool
definition, a missing or blank one is refused before anything is written, the stored
file carries the title with the kind ``document``, and the tool's answer names the
title and the file id. Server mode records the file in the user's area; personal mode
writes it to the allowed root and keeps a copy in the personal file area for its id.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark.database import files as files_db
from spark.files.area import FileArea, FileLimits
from spark.files.scope import Keeper, ToolScope
from spark.files.store import MountedVolumeStore
from spark.files.titles import TitleRefused
from spark.tools import document_creation

ALICE = "user-alice-0001"

INPUTS: dict[str, dict[str, Any]] = {
    "create_word": {"content": [{"type": "paragraph", "text": "Synthetic"}]},
    "create_excel": {"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]]}]},
    "create_powerpoint": {"slides": [{"layout": "title", "title": "Synthetic"}]},
    "create_pdf": {"content": [{"type": "paragraph", "text": "Synthetic"}]},
}
SUFFIX = {
    "create_word": ".docx",
    "create_excel": ".xlsx",
    "create_powerpoint": ".pptx",
    "create_pdf": ".pdf",
}
TOOLS = sorted(INPUTS)


@pytest.fixture
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


@pytest.fixture
def area(tmp_path: Path, bridge: Any) -> FileArea:
    return FileArea(
        MountedVolumeStore(tmp_path / "volume"),
        bridge,
        lambda: FileLimits(max_upload_bytes=None, quota_bytes=None),
    )


def _file_id(text: str) -> int:
    match = re.search(r"\bfile (\d+)\b", text)
    assert match, text
    return int(match.group(1))


def test_every_create_tool_requires_a_title() -> None:
    for tool in document_creation.get_tools():
        schema = tool["inputSchema"]
        assert "title" in schema["required"], tool["name"]
        assert schema["properties"]["title"]["type"] == "string"


@pytest.mark.parametrize("tool", TOOLS)
def test_server_records_the_title_and_returns_the_file_id(
    tool: str, area: FileArea, bridge: Any
) -> None:
    scope = ToolScope.server(area, ALICE, conversation_id=None)
    key = f"reports/out{SUFFIX[tool]}"
    out = document_creation.execute(
        tool, {"path": key, "title": "Quarterly <b>review</b>", **INPUTS[tool]}, scope=scope
    )
    fid = _file_id(out)
    assert "Quarterly ＜b＞review＜/b＞" in out and key in out
    row = bridge.call(files_db.get_user_file, ALICE, fid)
    assert row is not None
    assert (row["storage_key"], row["title"], row["kind"]) == (
        key,
        "Quarterly ＜b＞review＜/b＞",
        "document",
    )


@pytest.mark.parametrize("title", [None, "", "  \n ", 7, "x" * 121])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_missing_blank_or_long_title_is_refused_before_writing(
    tool: str, title: Any, area: FileArea, bridge: Any
) -> None:
    scope = ToolScope.server(area, ALICE, conversation_id=None)
    args: dict[str, Any] = {"path": f"x{SUFFIX[tool]}", **INPUTS[tool]}
    if title is not None:
        args["title"] = title
    with pytest.raises(TitleRefused, match="title"):
        document_creation.execute(tool, args, scope=scope)
    assert area.store.list(ALICE) == []
    assert bridge.call(files_db.list_user_files, ALICE) == []


@pytest.mark.parametrize("tool", TOOLS)
def test_personal_keeps_a_copy_with_its_title_for_the_file_id(
    tool: str, area: FileArea, bridge: Any, tmp_path: Path
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    scope = ToolScope.personal([root], keeper=Keeper(area, ALICE, None))
    target = root / f"report{SUFFIX[tool]}"
    out = document_creation.execute(
        tool, {"path": str(target), "title": "Report", **INPUTS[tool]}, scope=scope
    )
    assert target.is_file()
    fid = _file_id(out)
    row = bridge.call(files_db.get_user_file, ALICE, fid)
    assert row is not None and (row["title"], row["kind"]) == ("Report", "document")
    kept = area.store.local_path(ALICE, str(row["storage_key"]))
    assert kept.read_bytes() == target.read_bytes()


def test_personal_without_a_file_area_has_no_file_id(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    scope = ToolScope.personal([root])
    out = document_creation.execute(
        "create_pdf",
        {"path": str(root / "a.pdf"), "title": "Report", **INPUTS["create_pdf"]},
        scope=scope,
    )
    assert (root / "a.pdf").is_file()
    assert '"Report"' in out
    assert not re.search(r"\bfile \d+", out)
