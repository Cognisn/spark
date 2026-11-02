"""The personal-mode filesystem tool stays inside the configured allowed roots.

Every path goes through the one resolved-path check in :mod:`spark.files.paths`
(``contain_any`` over the allowed roots), the rule the document and archive
tools already use. Every escape form in ``tests.test_files_paths.ESCAPES`` is
tried against every filesystem tool, directly and through the registry, and
the listing tools never show or follow an entry that lies outside the roots.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from spark.files.paths import PathRefused
from spark.files.scope import ToolScope, ToolScopeUnavailable
from spark.tools import filesystem
from spark.tools.registry import execute_builtin_tool
from tests.test_files_paths import ESCAPES, REASONS

ESCAPE_KEYS = [k for _, k in ESCAPES]
ESCAPE_IDS = [i for i, _ in ESCAPES]
SECRET = "synthetic secret outside the root"

# Every filesystem tool with the inputs it needs besides its path.
TOOL_INPUTS: dict[str, dict[str, Any]] = {
    "read_file": {},
    "list_directory": {"recursive": True},
    "search_files": {"pattern": "*"},
    "get_file_info": {},
    "find_in_file": {"query": "synthetic"},
    "get_directory_tree": {},
    "write_file": {"content": "synthetic"},
}
TOOLS = list(TOOL_INPUTS)


def _symlink(link: Path, target: Path, *, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:  # no symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")


# The helpers that touch the filesystem, with the position of their path argument.
_LEAVES = {
    "_read_file": 0,
    "_get_file_info": 0,
    "_find_in_file": 0,
    "_write_file": 0,
    "_list_directory": 1,
    "_search_files": 1,
    "_get_directory_tree": 1,
}


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test inside ``tmp_path`` even when the guard under test is broken.

    These tests hand real escape forms (system files, other drives, network
    shares) to tools that can write. If containment is ever broken, for example
    while mutation-checking it, a leaf helper given a path outside ``tmp_path``
    fails the test without touching that path, network and device paths are
    never resolved, and relative paths land in ``tmp_path``.
    """
    base = tmp_path.resolve()
    monkeypatch.chdir(tmp_path)

    def barrier(name: str, index: int) -> Any:
        real = getattr(filesystem, name)

        def guarded(*args: Any, **kwargs: Any) -> Any:
            path = Path(args[index])
            if not path.is_absolute() or not path.is_relative_to(base):
                raise AssertionError(f"{name} escaped the sandbox: {path}")
            return real(*args, **kwargs)

        return guarded

    for name, index in _LEAVES.items():
        monkeypatch.setattr(filesystem, name, barrier(name, index))

    realpath = os.path.realpath

    def local_realpath(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path)[:2] in ("\\\\", "//", "\\/", "/\\"):
            raise AssertionError(f"network or device path resolved: {path}")
        return realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", local_realpath)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "allowed"
    path.mkdir()
    (path / "notes.txt").write_text("synthetic notes inside the root\n", encoding="utf-8")
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory beside the root holding a file the tool must never reach."""
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_text(SECRET + "\n", encoding="utf-8")
    return path


@pytest.fixture
def scope(root: Path) -> ToolScope:
    return ToolScope.personal([root])


def _run(tool: str, path: str, scope: ToolScope) -> str:
    return filesystem.execute(
        tool, {"path": path, **TOOL_INPUTS[tool]}, scope=scope, mode="read_write"
    )


def _config(root: Path, *more: Path) -> dict[str, Any]:
    return {
        "embedded_tools": {
            "filesystem": {
                "enabled": True,
                "mode": "read_write",
                "allowed_paths": [str(root), *(str(p) for p in more)],
            }
        }
    }


def _expected_reason(form: str) -> str:
    """The rule that refuses ``form`` through ``contain_any`` on this host.

    The host's own absolute form is accepted as text and refused because it
    lies outside the roots; every other form is refused by its text rule.
    """
    host_absolute = (
        ("drive-absolute", "drive-absolute-slash")
        if sys.platform == "win32"
        else ("posix-absolute",)
    )
    if form in host_absolute:
        return "outside the permitted area"
    return REASONS.get(form, REASONS[form.split("-")[0]])


def _files_under(path: Path) -> set[Path]:
    return {p for p in path.rglob("*")}


class TestEscapeForms:
    """Each escape form is refused by every tool, before anything is read or written."""

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize("key", ESCAPE_KEYS, ids=ESCAPE_IDS)
    def test_tool_refuses_the_form(
        self, scope: ToolScope, tmp_path: Path, tool: str, key: str
    ) -> None:
        before = _files_under(tmp_path)
        with pytest.raises(PathRefused):
            _run(tool, key, scope)
        assert _files_under(tmp_path) == before

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize("form", ESCAPE_IDS)
    def test_tool_refuses_the_form_by_its_own_rule(
        self, scope: ToolScope, tool: str, form: str
    ) -> None:
        """Each form is caught by the rule written for it, not only by the final resolved check."""
        key = dict(ESCAPES)[form]
        with pytest.raises(PathRefused) as info:
            _run(tool, key, scope)
        assert _expected_reason(form) in info.value.reason

    @pytest.mark.parametrize("tool", TOOLS)
    @pytest.mark.parametrize("key", ESCAPE_KEYS, ids=ESCAPE_IDS)
    def test_registry_reports_the_refusal(self, root: Path, tool: str, key: str) -> None:
        text, is_error = execute_builtin_tool(
            tool, {"path": key, **TOOL_INPUTS[tool]}, _config(root)
        )
        assert is_error is True
        assert text.startswith("Path refused: ")
        assert str(root) not in text


class TestOutsideTheRoots:
    @pytest.mark.parametrize("tool", TOOLS)
    def test_absolute_path_outside_every_root(
        self, scope: ToolScope, outside: Path, tool: str
    ) -> None:
        with pytest.raises(PathRefused, match="outside the permitted area"):
            _run(tool, str(outside / "secret.txt"), scope)

    @pytest.mark.parametrize("tool", TOOLS)
    def test_sibling_sharing_the_roots_prefix(
        self, scope: ToolScope, tmp_path: Path, tool: str
    ) -> None:
        sibling = tmp_path / "allowed-other"
        sibling.mkdir()
        (sibling / "secret.txt").write_text(SECRET, encoding="utf-8")
        with pytest.raises(PathRefused, match="outside the permitted area"):
            _run(tool, str(sibling / "secret.txt"), scope)
        assert (sibling / "secret.txt").read_text(encoding="utf-8") == SECRET

    @pytest.mark.parametrize("tool", TOOLS)
    def test_symlinked_file_out_of_the_root(
        self, scope: ToolScope, root: Path, outside: Path, tool: str
    ) -> None:
        _symlink(root / "innocent.txt", outside / "secret.txt", is_dir=False)
        with pytest.raises(PathRefused):
            _run(tool, str(root / "innocent.txt"), scope)
        assert (outside / "secret.txt").read_text(encoding="utf-8") == SECRET + "\n"

    @pytest.mark.parametrize("tool", TOOLS)
    def test_symlinked_directory_out_of_the_root(
        self, scope: ToolScope, root: Path, outside: Path, tool: str
    ) -> None:
        _symlink(root / "link", outside, is_dir=True)
        target = "link" if tool in ("list_directory", "get_directory_tree") else "link/secret.txt"
        with pytest.raises(PathRefused):
            _run(tool, str(root / target), scope)
        assert {p.name for p in outside.iterdir()} == {"secret.txt"}

    def test_parent_components_through_a_real_directory(self, scope: ToolScope, root: Path) -> None:
        (root / "sub").mkdir()
        with pytest.raises(PathRefused, match="parent directory"):
            _run("read_file", str(root / "sub" / ".." / ".." / "outside" / "secret.txt"), scope)

    def test_no_roots_means_no_filesystem_tool(self) -> None:
        with pytest.raises(ToolScopeUnavailable):
            ToolScope.personal([])

    def test_registry_without_allowed_paths_refuses_with_a_clear_message(self) -> None:
        text, is_error = execute_builtin_tool(
            "read_file", {"path": "notes.txt"}, {"embedded_tools": {"filesystem": {}}}
        )
        assert is_error is True
        assert "allowed_paths" in text


class TestInsideTheRoots:
    def test_absolute_path_inside_the_root(self, scope: ToolScope, root: Path) -> None:
        assert "inside the root" in _run("read_file", str(root / "notes.txt"), scope)

    def test_relative_path_is_taken_against_the_first_root(self, scope: ToolScope) -> None:
        assert "inside the root" in _run("read_file", "notes.txt", scope)

    def test_default_path_is_the_first_root(self, scope: ToolScope) -> None:
        out = filesystem.execute("list_directory", {}, scope=scope)
        assert "notes.txt" in out

    def test_absolute_path_inside_a_second_root(self, root: Path, tmp_path: Path) -> None:
        second = tmp_path / "second"
        second.mkdir()
        (second / "b.txt").write_text("synthetic second root", encoding="utf-8")
        out = filesystem.execute(
            "read_file", {"path": str(second / "b.txt")}, scope=ToolScope.personal([root, second])
        )
        assert "second root" in out

    def test_symlink_inside_the_root_is_followed(self, scope: ToolScope, root: Path) -> None:
        _symlink(root / "alias.txt", root / "notes.txt", is_dir=False)
        assert "inside the root" in _run("read_file", str(root / "alias.txt"), scope)

    def test_backslash_separates_on_every_host(self, scope: ToolScope, root: Path) -> None:
        (root / "sub").mkdir()
        (root / "sub" / "x.txt").write_text("synthetic nested", encoding="utf-8")
        assert "nested" in _run("read_file", "sub\\x.txt", scope)

    def test_write_lands_inside_the_root(self, scope: ToolScope, root: Path) -> None:
        _run("write_file", "new/out.txt", scope)
        assert (root / "new" / "out.txt").read_text(encoding="utf-8") == "synthetic"

    def test_registry_reads_inside_the_root(self, root: Path) -> None:
        text, is_error = execute_builtin_tool("read_file", {"path": "notes.txt"}, _config(root))
        assert is_error is False
        assert "inside the root" in text


class TestListingsStayInside:
    """Listing and search tools never show or follow an entry outside the roots."""

    @pytest.mark.parametrize(
        ("tool", "extra"),
        [
            ("list_directory", {"recursive": False}),
            ("list_directory", {"recursive": True}),
            ("get_directory_tree", {}),
            ("search_files", {"pattern": "*"}),
        ],
    )
    def test_symlinked_directory_is_not_followed(
        self, scope: ToolScope, root: Path, outside: Path, tool: str, extra: dict[str, Any]
    ) -> None:
        _symlink(root / "link", outside, is_dir=True)
        out = filesystem.execute(tool, {"path": str(root), **extra}, scope=scope)
        assert "secret.txt" not in out
        assert "link" not in out
        assert "notes.txt" in out

    @pytest.mark.parametrize(
        ("tool", "extra"),
        [
            ("list_directory", {"recursive": False}),
            ("list_directory", {"recursive": True}),
            ("get_directory_tree", {}),
            ("search_files", {"pattern": "*.txt"}),
        ],
    )
    def test_symlinked_file_is_not_listed(
        self, scope: ToolScope, root: Path, outside: Path, tool: str, extra: dict[str, Any]
    ) -> None:
        _symlink(root / "innocent.txt", outside / "secret.txt", is_dir=False)
        out = filesystem.execute(tool, {"path": str(root), **extra}, scope=scope)
        assert "innocent.txt" not in out
        assert "notes.txt" in out

    @pytest.mark.parametrize(
        "pattern",
        [
            "../*",
            "../outside/*",
            "../**/*",
            "..\\outside\\*",
            "sub/../../outside/*",
            "/*",
            "\\*",
            "C:\\*",
            "C:*",
            "\\\\server\\share\\*",
        ],
    )
    def test_search_pattern_cannot_leave_the_directory(
        self, scope: ToolScope, root: Path, outside: Path, pattern: str
    ) -> None:
        (root / "sub").mkdir()
        with pytest.raises(PathRefused):
            filesystem.execute("search_files", {"path": str(root), "pattern": pattern}, scope=scope)

    def test_search_from_a_file_root_cannot_reach_its_parent(self, root: Path) -> None:
        """A search given a file searches its directory, which must itself be inside the roots."""
        only_file = ToolScope.personal([root / "notes.txt"])
        (root / "beside.txt").write_text("synthetic", encoding="utf-8")
        with pytest.raises(PathRefused):
            filesystem.execute(
                "search_files", {"path": str(root / "notes.txt"), "pattern": "*"}, scope=only_file
            )

    def test_search_pattern_inside_the_directory(self, scope: ToolScope, root: Path) -> None:
        (root / "sub").mkdir()
        (root / "sub" / "deep.txt").write_text("synthetic", encoding="utf-8")
        out = filesystem.execute(
            "search_files", {"path": str(root), "pattern": "sub/*.txt"}, scope=scope
        )
        assert "deep.txt" in out
