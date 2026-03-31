"""Tests for built-in tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from spark.tools.archives import get_tools as archive_tools
from spark.tools.datetime_tool import TOOLS as dt_tools
from spark.tools.datetime_tool import execute as dt_execute
from spark.tools.documents import get_tools as doc_tools
from spark.tools.filesystem import execute as fs_execute
from spark.tools.filesystem import get_tools as fs_tools
from spark.tools.registry import execute_builtin_tool, get_builtin_tools
from spark.tools.web import get_tools as web_tools

# -- Registry -----------------------------------------------------------------


class TestRegistry:
    def test_get_all_tools_with_paths(self) -> None:
        config = {
            "embedded_tools": {
                "filesystem": {"enabled": True, "mode": "read", "allowed_paths": ["/tmp"]},
                "documents": {"enabled": True, "mode": "read"},
                "archives": {"enabled": True, "mode": "list"},
                "web": {"enabled": True},
            }
        }
        tools = get_builtin_tools(config)
        assert len(tools) > 0
        names = {t["name"] for t in tools}
        assert "get_current_datetime" in names
        assert "read_file" in names
        assert "web_search" in names

    def test_filesystem_excluded_without_paths(self) -> None:
        config = {
            "embedded_tools": {
                "filesystem": {"enabled": True, "mode": "read", "allowed_paths": []},
                "documents": {"enabled": True},
                "archives": {"enabled": True},
                "web": {"enabled": True},
            }
        }
        tools = get_builtin_tools(config)
        names = {t["name"] for t in tools}
        assert "get_current_datetime" in names
        assert "web_search" in names
        # Filesystem, documents, archives all excluded — no paths
        assert "read_file" not in names
        assert "read_word" not in names
        assert "list_archive" not in names

    def test_disabled_tools_excluded(self) -> None:
        config = {
            "embedded_tools": {
                "filesystem": {"enabled": False},
                "documents": {"enabled": False},
                "archives": {"enabled": False},
                "web": {"enabled": False},
            }
        }
        tools = get_builtin_tools(config)
        names = {t["name"] for t in tools}
        assert "get_current_datetime" in names
        assert "read_file" not in names

    def test_read_write_mode_includes_write(self) -> None:
        config = {
            "embedded_tools": {
                "filesystem": {"enabled": True, "mode": "read_write", "allowed_paths": ["/tmp"]},
                "documents": {"enabled": False},
                "archives": {"enabled": False},
                "web": {"enabled": False},
            }
        }
        tools = get_builtin_tools(config)
        names = {t["name"] for t in tools}
        assert "write_file" in names

    def test_execute_unknown_tool(self) -> None:
        result, is_error = execute_builtin_tool("nonexistent_tool", {}, {})
        assert is_error is True
        assert "Unknown" in result

    def test_execute_datetime(self) -> None:
        result, is_error = execute_builtin_tool("get_current_datetime", {}, {})
        assert is_error is False
        assert "Current date/time" in result

    def test_execute_filesystem_without_paths(self) -> None:
        result, is_error = execute_builtin_tool(
            "read_file", {"path": "/tmp/x"}, {"embedded_tools": {}}
        )
        assert is_error is True
        assert "allowed_paths" in result

    def test_tool_definitions_have_schema(self) -> None:
        config = {
            "embedded_tools": {
                "filesystem": {"enabled": True, "allowed_paths": ["/tmp"]},
                "documents": {"enabled": True},
                "archives": {"enabled": True},
                "web": {"enabled": True},
            }
        }
        tools = get_builtin_tools(config)
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool


# -- DateTime -----------------------------------------------------------------


class TestDatetime:
    def test_tool_definition(self) -> None:
        assert len(dt_tools) == 1
        assert dt_tools[0]["name"] == "get_current_datetime"

    def test_default_utc(self) -> None:
        result = dt_execute({})
        assert "UTC" in result
        assert "Current date/time" in result

    def test_custom_timezone(self) -> None:
        result = dt_execute({"timezone": "America/New_York"})
        assert "America/New_York" in result

    def test_human_format(self) -> None:
        result = dt_execute({"format": "human"})
        # Human format includes day name
        assert any(
            d in result
            for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        )

    def test_invalid_timezone_falls_back(self) -> None:
        result = dt_execute({"timezone": "Invalid/Zone"})
        assert "UTC" in result


# -- Filesystem ---------------------------------------------------------------


class TestFilesystem:
    def test_read_tools(self) -> None:
        tools = fs_tools(mode="read")
        names = {t["name"] for t in tools}
        assert "read_file" in names
        assert "write_file" not in names

    def test_readwrite_tools(self) -> None:
        tools = fs_tools(mode="read_write")
        names = {t["name"] for t in tools}
        assert "write_file" in names

    def test_read_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = fs_execute("read_file", {"path": str(f)}, allowed_paths=[str(tmp_path)])
        assert "hello world" in result

    def test_read_file_not_found(self, tmp_path: Path) -> None:
        result = fs_execute(
            "read_file", {"path": str(tmp_path / "nope.txt")}, allowed_paths=[str(tmp_path)]
        )
        assert "not found" in result.lower()

    def test_read_file_max_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "long.txt"
        f.write_text("\n".join(f"line {i}" for i in range(100)))
        result = fs_execute(
            "read_file", {"path": str(f), "max_lines": 5}, allowed_paths=[str(tmp_path)]
        )
        assert "showing first 5" in result.lower()

    def test_list_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").touch()
        (tmp_path / "subdir").mkdir()
        result = fs_execute(
            "list_directory", {"path": str(tmp_path)}, allowed_paths=[str(tmp_path)]
        )
        assert "a.txt" in result
        assert "subdir" in result

    def test_search_files(self, tmp_path: Path) -> None:
        (tmp_path / "test.py").touch()
        (tmp_path / "test.txt").touch()
        result = fs_execute(
            "search_files",
            {"path": str(tmp_path), "pattern": "*.py"},
            allowed_paths=[str(tmp_path)],
        )
        assert "test.py" in result
        assert "test.txt" not in result

    def test_get_file_info(self, tmp_path: Path) -> None:
        f = tmp_path / "info.txt"
        f.write_text("content")
        result = fs_execute("get_file_info", {"path": str(f)}, allowed_paths=[str(tmp_path)])
        assert "file" in result.lower()
        assert "bytes" in result

    def test_find_in_file(self, tmp_path: Path) -> None:
        f = tmp_path / "search.txt"
        f.write_text("Hello World\nFoo Bar\nHello Again\n")
        result = fs_execute(
            "find_in_file", {"path": str(f), "query": "Hello"}, allowed_paths=[str(tmp_path)]
        )
        assert "2 match" in result

    def test_directory_tree(self, tmp_path: Path) -> None:
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file.txt").touch()
        result = fs_execute(
            "get_directory_tree", {"path": str(tmp_path)}, allowed_paths=[str(tmp_path)]
        )
        assert "subdir" in result
        assert "file.txt" in result

    def test_write_file(self, tmp_path: Path) -> None:
        f = tmp_path / "out.txt"
        result = fs_execute(
            "write_file",
            {"path": str(f), "content": "new content"},
            allowed_paths=[str(tmp_path)],
            mode="read_write",
        )
        assert "Written" in result
        assert f.read_text() == "new content"

    def test_write_file_denied(self, tmp_path: Path) -> None:
        f = tmp_path / "out.txt"
        result = fs_execute(
            "write_file",
            {"path": str(f), "content": "x"},
            allowed_paths=[str(tmp_path)],
            mode="read",
        )
        assert "not enabled" in result.lower()

    def test_path_outside_allowed(self, tmp_path: Path) -> None:
        result = fs_execute("read_file", {"path": "/etc/passwd"}, allowed_paths=[str(tmp_path)])
        assert "denied" in result.lower()


# -- Documents ----------------------------------------------------------------


class TestDocuments:
    def test_tool_count(self) -> None:
        tools = doc_tools()
        assert len(tools) == 4
        names = {t["name"] for t in tools}
        assert "read_word" in names
        assert "read_excel" in names
        assert "read_pdf" in names
        assert "read_powerpoint" in names

    def test_file_not_found(self) -> None:
        from spark.tools.documents import execute

        result = execute("read_word", {"path": "/nonexistent/file.docx"})
        assert "not found" in result.lower()


# -- Archives -----------------------------------------------------------------


class TestArchives:
    def test_list_mode_tools(self) -> None:
        tools = archive_tools(mode="list")
        names = {t["name"] for t in tools}
        assert "list_archive" in names
        assert "extract_archive" not in names

    def test_extract_mode_tools(self) -> None:
        tools = archive_tools(mode="extract")
        names = {t["name"] for t in tools}
        assert "extract_archive" in names

    def test_list_zip(self, tmp_path: Path) -> None:
        import zipfile

        zp = tmp_path / "test.zip"
        with zipfile.ZipFile(str(zp), "w") as zf:
            zf.writestr("hello.txt", "hello world")
            zf.writestr("sub/file.txt", "content")

        from spark.tools.archives import execute

        result = execute("list_archive", {"path": str(zp)})
        assert "hello.txt" in result
        assert "2 files" in result

    def test_extract_denied(self, tmp_path: Path) -> None:
        import zipfile

        zp = tmp_path / "test.zip"
        with zipfile.ZipFile(str(zp), "w") as zf:
            zf.writestr("file.txt", "content")

        from spark.tools.archives import execute

        result = execute(
            "extract_archive", {"path": str(zp), "destination": str(tmp_path / "out")}, mode="list"
        )
        assert "not enabled" in result.lower()


# -- Web tools ----------------------------------------------------------------


class TestWeb:
    def test_tool_count(self) -> None:
        tools = web_tools()
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert "web_search" in names
        assert "web_fetch" in names
