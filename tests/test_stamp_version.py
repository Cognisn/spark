"""Tests for scripts/stamp_version.py."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "stamp_version.py"
_spec = importlib.util.spec_from_file_location("stamp_version", _SCRIPT)
stamp_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stamp_version)

CHANGELOG = (
    "# Changelog\n"
    "\n"
    "## [Unreleased]\n"
    "\n"
    "### Added\n"
    "- A new thing\n"
    "\n"
    "## [0.2.0b17] - 2026-07-26\n"
    "\n"
    "### Fixed\n"
    "- An old thing\n"
)

EMPTY_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-10-28\n\n- Released\n"


def _repo(tmp_path: Path, version: str, changelog: str) -> Path:
    (tmp_path / "src" / "spark").mkdir(parents=True)
    (tmp_path / "src" / "spark" / "_version.txt").write_bytes(f"{version}\n".encode())
    (tmp_path / "CHANGELOG.md").write_bytes(changelog.encode())
    return tmp_path


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


class TestCheckFollows:
    @pytest.mark.parametrize(
        "current,new",
        [
            ("0.2.0b17", "0.2.0"),
            ("0.2.0b17", "0.2.0b18"),
            ("0.2.0b17", "0.2.0rc1"),
            ("0.2.0", "0.3.0a0"),
            ("0.3.0a0", "0.3.0a1"),
            ("0.2.0", "0.2.1"),
        ],
    )
    def test_accepts_a_later_version(self, current: str, new: str) -> None:
        stamp_version.check_follows(current, new)

    @pytest.mark.parametrize(
        "current,new",
        [
            ("0.2.0b17", "0.2.0b17"),
            ("0.2.0b17", "0.2.0b16"),
            ("0.2.0", "0.2.0rc1"),
            ("0.2.0", "0.1.9"),
            ("0.3.0a1", "0.3.0a0"),
        ],
    )
    def test_refuses_an_equal_or_earlier_version(self, current: str, new: str) -> None:
        with pytest.raises(stamp_version.StampError, match="does not follow"):
            stamp_version.check_follows(current, new)

    @pytest.mark.parametrize("new", ["not-a-version", "0.2.0-beta-x", ""])
    def test_refuses_an_invalid_version(self, new: str) -> None:
        with pytest.raises(stamp_version.StampError, match="not a valid PEP 440"):
            stamp_version.check_follows("0.2.0b17", new)

    @pytest.mark.parametrize("new", ["v0.2.0", "0.2.0-b18", "0.2.0.beta18", "0.2.0B18"])
    def test_refuses_a_non_canonical_version(self, new: str) -> None:
        with pytest.raises(stamp_version.StampError, match="canonical"):
            stamp_version.check_follows("0.2.0b17", new)

    def test_refuses_a_local_version(self) -> None:
        with pytest.raises(stamp_version.StampError, match="local"):
            stamp_version.check_follows("0.2.0b17", "0.2.0+build1")


class TestRollChangelog:
    def test_moves_unreleased_into_a_dated_section(self) -> None:
        rolled = stamp_version.roll_changelog(CHANGELOG, "0.2.0", "2026-10-28")
        assert rolled == (
            "# Changelog\n"
            "\n"
            "## [Unreleased]\n"
            "\n"
            "## [0.2.0] - 2026-10-28\n"
            "\n"
            "### Added\n"
            "- A new thing\n"
            "\n"
            "## [0.2.0b17] - 2026-07-26\n"
            "\n"
            "### Fixed\n"
            "- An old thing\n"
        )

    def test_handles_unreleased_as_the_last_section(self) -> None:
        text = "# Changelog\n\n## [Unreleased]\n\n- Only entry\n"
        rolled = stamp_version.roll_changelog(text, "0.1.0", "2026-10-28")
        assert (
            rolled == "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-28\n\n- Only entry\n"
        )

    def test_leaves_an_empty_unreleased_alone(self) -> None:
        assert stamp_version.roll_changelog(EMPTY_CHANGELOG, "0.3.0a0", "2026-10-28") == (
            EMPTY_CHANGELOG
        )

    def test_refuses_an_existing_section(self) -> None:
        with pytest.raises(stamp_version.StampError, match="already has a section"):
            stamp_version.roll_changelog(CHANGELOG, "0.2.0b17", "2026-10-28")

    def test_refuses_a_changelog_without_unreleased(self) -> None:
        with pytest.raises(stamp_version.StampError, match="no '## \\[Unreleased\\]'"):
            stamp_version.roll_changelog("# Changelog\n\n## [0.1.0] - x\n", "0.2.0", "d")


class TestStamp:
    def test_writes_version_and_changelog_with_lf(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, "0.2.0b17", CHANGELOG.replace("\n", "\r\n"))
        assert stamp_version.stamp(root, "0.2.0", "2026-10-28") is True

        version = (root / "src" / "spark" / "_version.txt").read_bytes()
        assert version == b"0.2.0\n"
        changelog = _read(root / "CHANGELOG.md")
        assert "\r" not in changelog
        assert "## [Unreleased]\n\n## [0.2.0] - 2026-10-28\n\n### Added\n" in changelog

    def test_empty_unreleased_stamps_only_the_version(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, "0.2.0", EMPTY_CHANGELOG)
        assert stamp_version.stamp(root, "0.3.0a0", "2026-10-28") is False
        assert (root / "src" / "spark" / "_version.txt").read_bytes() == b"0.3.0a0\n"
        assert _read(root / "CHANGELOG.md") == EMPTY_CHANGELOG

    def test_refusal_writes_nothing(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, "0.2.0", CHANGELOG)
        with pytest.raises(stamp_version.StampError):
            stamp_version.stamp(root, "0.2.0rc1", "2026-10-28")
        assert (root / "src" / "spark" / "_version.txt").read_bytes() == b"0.2.0\n"
        assert _read(root / "CHANGELOG.md") == CHANGELOG


class TestMain:
    def test_success_returns_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        root = _repo(tmp_path, "0.2.0b17", CHANGELOG)
        code = stamp_version.main(["0.2.0", "--date", "2026-10-28", "--root", str(root)])
        assert code == 0
        assert "Stamped 0.2.0" in capsys.readouterr().out

    def test_refusal_returns_two(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        root = _repo(tmp_path, "0.2.0b17", CHANGELOG)
        code = stamp_version.main(["0.2.0b16", "--root", str(root)])
        assert code == 2
        assert "Refused" in capsys.readouterr().err

    def test_bad_date_returns_two(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        root = _repo(tmp_path, "0.2.0b17", CHANGELOG)
        code = stamp_version.main(["0.2.0", "--date", "28/10/2026", "--root", str(root)])
        assert code == 2
        assert "YYYY-MM-DD" in capsys.readouterr().err
        assert (root / "src" / "spark" / "_version.txt").read_bytes() == b"0.2.0b17\n"
