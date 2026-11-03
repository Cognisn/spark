"""list_my_files and read_my_file: the signed-in user's own file area, nobody else's.

The tools read through ``spark.files`` for the turn's user (``_user_guid``) on
the bridge the turn carries. Guards under test:

* each tool lists and reads only its own user's files; another user's file id
  answers exactly as an id that does not exist;
* a turn without a user, or without the bridge, is refused (never the process
  user, never another user);
* personal mode reads the personal area (``<data dir>/files``) with no
  allowed_paths configured;
* the tools belong to the documents category, so a server offers and runs them
  only for a user holding ``spark.tools.documents``.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark import files as files_pkg
from spark.files.scope import ToolScopeUnavailable
from spark.mode import Mode
from spark.tools import my_files
from spark.tools.registry import (
    PERMISSIONS_KEY,
    TOOL_CATEGORIES,
    execute_builtin_tool,
    get_builtin_tools,
    tool_category,
)

ALICE = "user-alice-0001"
BOB = "user-bob-0002"


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


@pytest.fixture
def personal_files(tmp_path: Path) -> Iterator[Path]:
    files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
    yield tmp_path / "files"
    files_pkg.reset()


def _save(bridge: Any, user: str, name: str, data: bytes, cid: int | None = None) -> int:
    area = files_pkg.file_area(bridge)
    return area.save(
        user,
        f"uploads/{name}",
        io.BytesIO(data),
        filename=name,
        mime_type="text/plain",
        conversation_id=cid,
    )


def _config(bridge: Any, user: str | None, *, cid: int | None = None) -> dict[str, Any]:
    return {
        "_user_guid": user,
        "_kg_db": bridge,
        "_kg_conversation_id": cid,
        "_kg_user_guid": user,
    }


def _run(name: str, args: dict[str, Any], config: dict[str, Any]) -> tuple[str, bool]:
    return my_files.execute(name, args, config)


class TestCatalogue:
    def test_both_tools_are_in_the_documents_category(self) -> None:
        assert tool_category("list_my_files") == "documents"
        assert tool_category("read_my_file") == "documents"
        assert {"list_my_files", "read_my_file"} <= set(TOOL_CATEGORIES["documents"])

    def test_definitions_name_the_inputs(self) -> None:
        tools = {t["name"]: t for t in my_files.get_tools()}
        assert set(tools) == {"list_my_files", "read_my_file"}
        assert tools["read_my_file"]["inputSchema"]["required"] == ["file_id"]
        assert "file_id" in tools["read_my_file"]["inputSchema"]["properties"]

    def test_read_results_are_marked_untrusted_documents(self) -> None:
        from spark.safety.untrusted import SourceCategory, source_category

        assert source_category("read_my_file", mcp=False) is SourceCategory.DOCUMENTS
        assert source_category("list_my_files", mcp=False) is SourceCategory.DOCUMENTS


class TestList:
    def test_lists_only_the_users_own_files(self, bridge: Any, server_files: Path) -> None:
        mine = _save(bridge, ALICE, "alice-notes.txt", b"alpha")
        _save(bridge, BOB, "bob-secret.txt", b"bravo")

        text, is_error = _run("list_my_files", {}, _config(bridge, ALICE))

        assert not is_error, text
        assert f"file {mine}: alice-notes.txt" in text
        assert "bob-secret" not in text

    def test_says_so_when_there_are_none(self, bridge: Any, server_files: Path) -> None:
        _save(bridge, BOB, "bob-secret.txt", b"bravo")
        text, is_error = _run("list_my_files", {}, _config(bridge, ALICE))
        assert not is_error
        assert "no files" in text.lower()
        assert "bob" not in text

    def test_conversation_only_keeps_this_conversations_files(
        self, bridge: Any, server_files: Path
    ) -> None:
        from spark.database import conversations as convdb

        cid = bridge.call(convdb.create_conversation, "Synthetic", "stub-model", ALICE)
        here = _save(bridge, ALICE, "here.txt", b"in this one", cid)
        elsewhere = _save(bridge, ALICE, "elsewhere.txt", b"not in this one")

        text, _ = _run(
            "list_my_files", {"conversation_only": True}, _config(bridge, ALICE, cid=cid)
        )
        assert f"file {here}:" in text
        assert f"file {elsewhere}:" not in text

        text, _ = _run("list_my_files", {}, _config(bridge, ALICE, cid=cid))
        assert f"file {here}:" in text and f"file {elsewhere}:" in text


class TestRead:
    def test_reads_the_users_own_text_file(self, bridge: Any, server_files: Path) -> None:
        fid = _save(bridge, ALICE, "notes.txt", b"synthetic meeting notes from example.com")
        text, is_error = _run("read_my_file", {"file_id": fid}, _config(bridge, ALICE))
        assert not is_error, text
        assert "synthetic meeting notes from example.com" in text
        assert "notes.txt" in text

    def test_another_users_id_answers_as_not_found(self, bridge: Any, server_files: Path) -> None:
        theirs = _save(bridge, BOB, "bob-secret.txt", b"bravo secret words")
        missing = theirs + 1000

        refused, refused_error = _run("read_my_file", {"file_id": theirs}, _config(bridge, ALICE))
        absent, absent_error = _run("read_my_file", {"file_id": missing}, _config(bridge, ALICE))

        assert refused_error and absent_error
        assert "bravo" not in refused and "bob-secret" not in refused
        assert refused.replace(str(theirs), "N") == absent.replace(str(missing), "N")

    def test_another_users_id_is_not_found_even_when_the_same_key_is_in_my_area(
        self, bridge: Any, server_files: Path
    ) -> None:
        # Created documents are keyed by name, so two users can hold the same key.
        area = files_pkg.file_area(bridge)
        area.save(ALICE, "report.txt", io.BytesIO(b"alpha own words"), filename="alice-report.txt")
        theirs = area.save(
            BOB, "report.txt", io.BytesIO(b"bravo private words"), filename="bob-report.txt"
        )

        text, is_error = _run("read_my_file", {"file_id": theirs}, _config(bridge, ALICE))

        assert is_error
        assert "bob-report" not in text and "bravo" not in text and "alpha" not in text

    def test_accepts_a_numeric_string_id(self, bridge: Any, server_files: Path) -> None:
        fid = _save(bridge, ALICE, "notes.txt", b"words")
        text, is_error = _run("read_my_file", {"file_id": str(fid)}, _config(bridge, ALICE))
        assert not is_error and "words" in text

    @pytest.mark.parametrize("bad", [None, "", "abc", "1.5", -1, True, [1]])
    def test_refuses_an_unusable_id(self, bridge: Any, server_files: Path, bad: Any) -> None:
        text, is_error = _run("read_my_file", {"file_id": bad}, _config(bridge, ALICE))
        assert is_error
        assert "file_id" in text

    def test_pages_long_text(self, bridge: Any, server_files: Path) -> None:
        body = ("0123456789" * 50).encode()
        fid = _save(bridge, ALICE, "long.txt", body)

        first, _ = _run("read_my_file", {"file_id": fid, "max_chars": 100}, _config(bridge, ALICE))
        assert "0123456789" * 10 in first
        assert "offset 100" in first

        last, _ = _run(
            "read_my_file",
            {"file_id": fid, "offset": 450, "max_chars": 100},
            _config(bridge, ALICE),
        )
        assert "0123456789" * 5 in last
        assert "offset 550" not in last

    def test_refuses_binary_content(self, bridge: Any, server_files: Path) -> None:
        fid = _save(bridge, ALICE, "blob.bin", b"\x00\x01\x02binary\x00")
        text, is_error = _run("read_my_file", {"file_id": fid}, _config(bridge, ALICE))
        assert is_error
        assert "binary" in text.lower()

    def test_reads_a_word_document_through_the_document_reader(
        self, bridge: Any, server_files: Path
    ) -> None:
        from docx import Document

        buffer = io.BytesIO()
        doc = Document()
        doc.add_paragraph("Synthetic quarterly summary for Example Pty Ltd.")
        doc.save(buffer)
        fid = _save(bridge, ALICE, "summary.docx", buffer.getvalue())

        text, is_error = _run("read_my_file", {"file_id": fid}, _config(bridge, ALICE))
        assert not is_error, text
        assert "Synthetic quarterly summary for Example Pty Ltd." in text

    def test_a_record_whose_content_is_gone_is_not_found(
        self, bridge: Any, server_files: Path
    ) -> None:
        fid = _save(bridge, ALICE, "gone.txt", b"words")
        files_pkg.file_store().local_path(ALICE, "uploads/gone.txt").unlink()
        text, is_error = _run("read_my_file", {"file_id": fid}, _config(bridge, ALICE))
        assert is_error and "not found" in text.lower()


class TestWhoIsAsking:
    @pytest.mark.parametrize("tool", ["list_my_files", "read_my_file"])
    def test_refused_without_a_user(self, bridge: Any, server_files: Path, tool: str) -> None:
        _save(bridge, ALICE, "notes.txt", b"words")
        with pytest.raises(ToolScopeUnavailable):
            _run(tool, {"file_id": 1}, _config(bridge, None))

    @pytest.mark.parametrize("tool", ["list_my_files", "read_my_file"])
    def test_refused_without_the_bridge(self, server_files: Path, tool: str) -> None:
        with pytest.raises(ToolScopeUnavailable):
            _run(tool, {"file_id": 1}, {"_user_guid": ALICE})

    def test_the_turns_user_wins_over_the_knowledge_graph_user(
        self, bridge: Any, server_files: Path
    ) -> None:
        theirs = _save(bridge, BOB, "bob.txt", b"bravo")
        config = {**_config(bridge, ALICE), "_kg_user_guid": BOB}
        text, is_error = _run("read_my_file", {"file_id": theirs}, config)
        assert is_error and "bravo" not in text


class TestPersonalArea:
    def test_personal_mode_reads_the_personal_area_without_allowed_paths(
        self, bridge: Any, personal_files: Path
    ) -> None:
        fid = _save(bridge, ALICE, "notes.txt", b"personal words")
        assert any(personal_files.rglob("notes.txt"))

        text, is_error = execute_builtin_tool(
            "read_my_file", {"file_id": fid}, {**_config(bridge, ALICE), "embedded_tools": {}}
        )
        assert not is_error, text
        assert "personal words" in text

    def test_personal_mode_offers_the_tools_without_allowed_paths(
        self, personal_files: Path
    ) -> None:
        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert {"list_my_files", "read_my_file"} <= names
        assert "read_pdf" not in names  # the path-based readers still need allowed_paths


class TestRegistry:
    def test_not_offered_without_a_file_area(self) -> None:
        files_pkg.reset()
        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert "list_my_files" not in names and "read_my_file" not in names

    def test_not_offered_when_documents_are_disabled(self, personal_files: Path) -> None:
        config = {"embedded_tools": {"documents": {"enabled": False}}}
        names = {t["name"] for t in get_builtin_tools(config)}
        assert "list_my_files" not in names

    def test_server_refuses_a_user_without_the_documents_permission(
        self, bridge: Any, server_files: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spark import mode

        monkeypatch.setattr(mode, "current_mode", lambda: Mode.SERVER)
        monkeypatch.setattr(mode, "is_server", lambda: True)
        fid = _save(bridge, ALICE, "notes.txt", b"words")
        config = {**_config(bridge, ALICE), PERMISSIONS_KEY: frozenset({"spark.tools.web"})}

        names = {t["name"] for t in get_builtin_tools(config)}
        assert "read_my_file" not in names
        text, is_error = execute_builtin_tool("read_my_file", {"file_id": fid}, config)
        assert is_error and "words" not in text

        allowed = {**config, PERMISSIONS_KEY: frozenset({"spark.tools.documents"})}
        assert "read_my_file" in {t["name"] for t in get_builtin_tools(allowed)}
        text, is_error = execute_builtin_tool("read_my_file", {"file_id": fid}, allowed)
        assert not is_error and "words" in text

    def test_registry_reports_a_missing_user_as_a_tool_error(
        self, bridge: Any, server_files: Path
    ) -> None:
        text, is_error = execute_builtin_tool("list_my_files", {}, {"_kg_db": bridge})
        assert is_error
        assert "signed-in user" in text
