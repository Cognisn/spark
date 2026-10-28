"""Stamp a new Spark version.

Writes ``src/spark/_version.txt`` and moves the body of the ``[Unreleased]``
section of ``CHANGELOG.md`` into a new dated section for the version, leaving
``[Unreleased]`` empty. If ``[Unreleased]`` is already empty the changelog is
left as it is.

The new version must be a canonical PEP 440 version that sorts after the
current one, and must not already have a changelog section.

Usage::

    python scripts/stamp_version.py 0.2.0
    python scripts/stamp_version.py 0.3.0a0 --date 2026-10-28
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

VERSION_FILE = Path("src") / "spark" / "_version.txt"
CHANGELOG_FILE = Path("CHANGELOG.md")
UNRELEASED_HEADING = "## [Unreleased]"
SECTION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]", re.MULTILINE)


class StampError(Exception):
    """Raised when a stamp is refused."""


def parse_version(text: str) -> Version:
    """Parse ``text`` as a canonical PEP 440 version without a local part."""
    try:
        version = Version(text)
    except InvalidVersion as exc:
        raise StampError(f"'{text}' is not a valid PEP 440 version") from exc
    if version.local is not None:
        raise StampError(f"'{text}' has a local version part, which is not allowed")
    if str(version) != text:
        raise StampError(f"'{text}' is not in canonical form; use '{version}'")
    return version


def check_follows(current: str, new: str) -> None:
    """Refuse ``new`` unless it sorts strictly after ``current``."""
    new_version = parse_version(new)
    current_version = Version(current)
    if new_version <= current_version:
        raise StampError(f"{new} does not follow the current version {current}")


def roll_changelog(text: str, version: str, date: str) -> str:
    """Return ``text`` with the ``[Unreleased]`` body moved under ``version``."""
    for match in SECTION_HEADING.finditer(text):
        if match.group("version") == version:
            raise StampError(f"CHANGELOG.md already has a section for {version}")

    start = text.find(UNRELEASED_HEADING)
    if start == -1:
        raise StampError("CHANGELOG.md has no '## [Unreleased]' section")
    body_start = text.find("\n", start)
    body_start = len(text) if body_start == -1 else body_start + 1

    following = SECTION_HEADING.search(text, body_start)
    body_end = following.start() if following else len(text)
    body = text[body_start:body_end].strip("\n")
    if not body.strip():
        return text

    head = text[:body_start]
    tail = text[body_end:]
    section = f"## [{version}] - {date}\n\n{body}\n"
    return f"{head}\n{section}\n{tail}" if tail else f"{head}\n{section}"


def stamp(root: Path, version: str, date: str) -> bool:
    """Stamp ``version`` under ``root``. Returns True if the changelog changed."""
    version_path = root / VERSION_FILE
    changelog_path = root / CHANGELOG_FILE
    current = version_path.read_text(encoding="utf-8").strip()
    check_follows(current, version)

    changelog = changelog_path.read_text(encoding="utf-8")
    rolled = roll_changelog(changelog, version, date)

    version_path.write_text(f"{version}\n", encoding="utf-8", newline="\n")
    changed = rolled != changelog
    if changed:
        changelog_path.write_text(rolled, encoding="utf-8", newline="\n")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stamp a new Spark version.")
    parser.add_argument("version", help="the new PEP 440 version, for example 0.2.0 or 0.3.0a1")
    parser.add_argument(
        "--date",
        default=datetime.date.today().isoformat(),
        help="release date for the changelog section (default: today, YYYY-MM-DD)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the parent of this script's folder)",
    )
    args = parser.parse_args(argv)

    try:
        datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"Refused: '{args.date}' is not a YYYY-MM-DD date", file=sys.stderr)
        return 2

    try:
        changed = stamp(args.root, args.version, args.date)
    except StampError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2

    print(f"Stamped {args.version}")
    if changed:
        print(f"Moved [Unreleased] into [{args.version}] - {args.date}")
    else:
        print("[Unreleased] was empty; CHANGELOG.md left unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
