"""Path containment: one resolved-path check for user areas and allowed roots.

Every escape form is refused on every platform: the refusal is decided from
the text of the path before the host's path rules are applied, and the
resolved result must still lie inside the root (symlinks resolved).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from spark.files.paths import PathRefused, contain, contain_any

# (id, key): area-relative keys that must be refused on every host.
ESCAPES = [
    ("empty", ""),
    ("blank", "   "),
    ("parent", ".."),
    ("parent-prefix", "../outside.txt"),
    ("parent-nested", "a/../../outside.txt"),
    ("parent-backslash", "..\\outside.txt"),
    ("parent-mixed", "a\\..\\..\\outside.txt"),
    ("parent-trailing-dot", "a/.../x"),
    ("parent-trailing-space", "a/.. /x"),
    ("posix-absolute", "/etc/passwd"),
    ("rooted-backslash", "\\Windows\\win.ini"),
    ("drive-absolute", "C:\\Windows\\win.ini"),
    ("drive-absolute-slash", "C:/Windows/win.ini"),
    ("drive-relative", "C:win.ini"),
    ("drive-lower", "d:secret"),
    ("unc", "\\\\server\\share\\file.txt"),
    ("unc-slash", "//server/share/file.txt"),
    ("device-namespace", "\\\\.\\PhysicalDrive0"),
    ("long-path-namespace", "\\\\?\\C:\\Windows\\win.ini"),
    ("device-name", "CON"),
    ("device-name-ext", "reports/nul.txt"),
    ("device-name-com", "com1"),
    ("device-name-lpt", "LPT9.log"),
    ("device-conin", "CONIN$"),
    ("ads", "report.docx:hidden"),
    ("ads-stream-type", "report.docx::$DATA"),
    ("ads-dir", "dir:stream/file.txt"),
    ("trailing-dot", "secret.txt."),
    ("trailing-space", "secret.txt "),
    ("nul-byte", "a\x00b.txt"),
    ("control-char", "a\nb.txt"),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    area = tmp_path / "area"
    area.mkdir()
    return area


class TestContainKeys:
    @pytest.mark.parametrize("key", [k for _, k in ESCAPES], ids=[i for i, _ in ESCAPES])
    def test_escape_form_is_refused(self, root: Path, key: str) -> None:
        with pytest.raises(PathRefused):
            contain(root, key)

    @pytest.mark.parametrize(
        "key",
        ["report.docx", "uploads/abc/report.docx", "a\\b\\c.txt", "./x.txt", "a/./b.txt", "."],
    )
    def test_relative_key_resolves_inside_the_root(self, root: Path, key: str) -> None:
        resolved = contain(root, key)
        assert resolved.is_relative_to(root.resolve())

    def test_backslash_is_a_separator_on_every_host(self, root: Path) -> None:
        assert contain(root, "a\\b.txt") == root.resolve() / "a" / "b.txt"

    def test_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path: Path) -> None:
        root = tmp_path / "area"
        root.mkdir()
        (tmp_path / "area-evil").mkdir()
        with pytest.raises(PathRefused):
            contain_any([root], str(tmp_path / "area-evil" / "x.txt"))

    def test_refusal_names_the_reason_without_the_root(self, root: Path) -> None:
        with pytest.raises(PathRefused) as info:
            contain(root, "../x")
        assert str(root) not in str(info.value)


def _symlink(link: Path, target: Path, *, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:  # no symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")


class TestSymlinks:
    def test_symlinked_directory_out_of_the_root_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink(root / "link", outside, is_dir=True)
        with pytest.raises(PathRefused):
            contain(root, "link/secret.txt")

    def test_symlinked_file_out_of_the_root_is_refused(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "secret.txt"
        outside.write_text("synthetic", encoding="utf-8")
        _symlink(root / "innocent.txt", outside, is_dir=False)
        with pytest.raises(PathRefused):
            contain(root, "innocent.txt")

    def test_symlink_inside_the_root_is_allowed(self, root: Path) -> None:
        (root / "real").mkdir()
        _symlink(root / "alias", root / "real", is_dir=True)
        assert contain(root, "alias/x.txt") == (root / "real" / "x.txt").resolve()

    def test_symlinked_root_is_followed(self, tmp_path: Path) -> None:
        real = tmp_path / "real-root"
        real.mkdir()
        link = tmp_path / "link-root"
        _symlink(link, real, is_dir=True)
        assert contain(link, "x.txt") == real.resolve() / "x.txt"


class TestContainAny:
    """Personal mode: absolute paths are allowed when they lie inside a configured root."""

    def test_absolute_path_inside_a_root_is_allowed(self, root: Path) -> None:
        target = root / "docs" / "a.docx"
        assert contain_any([root], str(target)) == target.resolve()

    def test_absolute_path_inside_the_second_root_is_allowed(
        self, root: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        assert contain_any([root, other], str(other / "a.txt")) == (other / "a.txt").resolve()

    def test_relative_path_is_taken_against_the_first_root(self, root: Path) -> None:
        assert contain_any([root], "docs/a.docx") == root.resolve() / "docs" / "a.docx"

    def test_absolute_path_outside_every_root_is_refused(self, root: Path, tmp_path: Path) -> None:
        with pytest.raises(PathRefused):
            contain_any([root], str(tmp_path / "elsewhere.txt"))

    def test_absolute_path_with_parent_components_is_refused(self, root: Path) -> None:
        with pytest.raises(PathRefused):
            contain_any([root], str(root) + os.sep + ".." + os.sep + "x.txt")

    def test_no_roots_refuses_everything(self, root: Path) -> None:
        with pytest.raises(PathRefused):
            contain_any([], str(root / "a.txt"))

    @pytest.mark.parametrize(
        "path",
        [
            "\\\\server\\share\\file.txt",
            "//server/share/file.txt",
            "\\\\?\\C:\\Windows\\win.ini",
            "\\\\.\\PhysicalDrive0",
            "C:win.ini",
            "\\Windows\\win.ini",
        ],
        ids=["unc", "unc-slash", "long-path", "device", "drive-relative", "rooted"],
    )
    def test_network_device_and_partial_forms_are_refused(self, root: Path, path: str) -> None:
        with pytest.raises(PathRefused):
            contain_any([root], path)

    def test_alternate_data_stream_on_an_absolute_path_is_refused(self, root: Path) -> None:
        with pytest.raises(PathRefused):
            contain_any([root], str(root / "a.txt") + ":hidden")

    @pytest.mark.skipif(sys.platform != "win32", reason="drive letters are a Windows form")
    def test_windows_absolute_path_inside_a_root_is_allowed(self, root: Path) -> None:
        assert contain_any([root], str(root / "a.txt")) == (root / "a.txt").resolve()

    @pytest.mark.skipif(sys.platform == "win32", reason="a POSIX absolute path")
    def test_drive_form_is_not_absolute_on_posix(self, root: Path) -> None:
        with pytest.raises(PathRefused):
            contain_any([root], "C:\\Windows\\win.ini")
