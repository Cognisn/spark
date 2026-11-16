"""Skill discovery, validation, and jailed resource access.

Skills are folders containing SKILL.md (front matter: name, description, and the
optional fields conduit records) plus optional resource files. Bundled skills ship
read-only in the package; user skills live in the platform data directory. Content
truth is on disk.

Every SKILL.md is validated by conduit's parser (:func:`conduit.skills.parse_skill_md`),
the same rules the model's ``use_skill`` loads through, so the skills page never shows
as valid a skill the model could not load. Resource paths follow conduit's rule as well.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from conduit.skills import SkillInvalid, parse_skill_md
from conduit.skills.format import resource_path_refusal

from spark.skills.source import SparkSkillSource

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_MD_MAX = 64 * 1024
RESOURCE_READ_MAX = 256 * 1024

#: conduit's refusal reasons, in words for the skills page (the code follows in brackets).
_REASONS = {
    "front_matter_missing": "missing front matter (a first line of ---)",
    "front_matter_unclosed": "the front matter is not closed with ---",
    "name_missing": "frontmatter name is required",
    "name_invalid": "frontmatter name must be kebab-case, max 64 chars",
    "description_missing": "description is required (1 to 1024 characters, one line)",
    "description_invalid": "description is required (1 to 1024 characters, one line)",
    "field_unknown": "unknown front matter field",
    "field_repeated": "a front matter field is repeated",
    "block_scalar_unsupported": "block values (| or >) are not supported in front matter",
    "value_invalid": "a front matter value is not valid",
    "line_invalid": "a front matter line is not valid",
    "version_invalid": "version must be a short label without spaces",
    "license_invalid": "license must be short single-line text",
    "compatibility_invalid": "compatibility must be short single-line text",
    "allowed_tools_invalid": "allowed-tools must be a list of tool names",
    "metadata_invalid": "metadata must be short key: value lines",
    "body_empty": "the instructions (the body after the front matter) are empty",
}


class SkillValidationError(Exception):
    """Raised when authored skill input is invalid."""


class SkillResourceError(Exception):
    """Raised on resource jail, size, or type violations."""


def refusal_text(error: SkillInvalid, folder: str) -> str:
    """conduit's refusal of a SKILL.md, in words (never the text it found)."""
    if error.reason == "name_mismatch":
        words = f"frontmatter name does not match folder '{folder}'"
    else:
        words = _REASONS.get(error.reason, "SKILL.md is not valid")
    at = f" at line {error.line}" if error.line is not None else ""
    return f"{words}{at} ({error.reason})"


def _parse_skill_dir(path: Path, source: str) -> dict[str, Any]:
    """Parse one skill folder into a record; invalid records carry an error."""
    record: dict[str, Any] = {
        "name": path.name,
        "description": "",
        "source": source,
        "path": str(path),
        "valid": False,
        "error": None,
    }
    md = path / "SKILL.md"
    if not md.is_file():
        record["error"] = "SKILL.md is missing"
        return record
    try:
        if md.stat().st_size > SKILL_MD_MAX:
            record["error"] = f"SKILL.md exceeds {SKILL_MD_MAX // 1024}KB"
            return record
        text = md.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - unreadable file must not break the scan
        record["error"] = f"SKILL.md unreadable: {type(e).__name__}"
        return record
    try:
        document = parse_skill_md(text, expected_name=path.name)
    except SkillInvalid as refused:
        record["error"] = refusal_text(refused, path.name)
        return record
    record.update(
        {
            "name": document.name,
            "description": document.description,
            "version": document.version,
            "valid": True,
        }
    )
    return record


class SkillsManager:
    """Discovers skills from the bundled and user directories."""

    def __init__(self, user_dir: Path, bundled_dir: Path | None = None) -> None:
        self._user_dir = Path(user_dir)
        self._bundled_dir = Path(bundled_dir) if bundled_dir else None
        try:
            self._user_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001 - never crash startup over the skills dir
            logger.warning("Skills user directory unavailable: %s", e)
        self._skills: dict[str, dict[str, Any]] = {}
        self._invalid: list[dict[str, Any]] = []
        self.reload()

    @property
    def user_dir(self) -> Path:
        return self._user_dir

    @property
    def bundled_dir(self) -> Path | None:
        return self._bundled_dir

    def source(self, enabled: Iterable[str] | None = None) -> SparkSkillSource:
        """These skills as a conduit skill source, limited to ``enabled`` names (a user's)."""
        return SparkSkillSource(self._user_dir, self._bundled_dir, enabled=enabled)

    def folders(self) -> dict[str, Path]:
        """Each valid skill's folder, by name."""
        return {name: Path(record["path"]) for name, record in self._skills.items()}

    def reload(self) -> None:
        """Rescan both sources. Bundled wins name collisions; user duplicate is invalid."""
        self._skills, self._invalid = {}, []
        for source, root in (("bundled", self._bundled_dir), ("user", self._user_dir)):
            if not root:
                continue
            try:
                entries = sorted(p for p in root.iterdir() if p.is_dir())
            except OSError as e:
                logger.warning("Cannot scan %s skills directory: %s", source, e)
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue  # staging and backup folders of an import or an edit
                record = _parse_skill_dir(entry, source)
                if record["valid"] and record["name"] in self._skills:
                    record["valid"] = False
                    record["error"] = "name collides with a bundled skill"
                if record["valid"]:
                    self._skills[record["name"]] = record
                else:
                    self._invalid.append(record)

    def list_skills(self, include_invalid: bool = False) -> list[dict[str, Any]]:
        out = sorted(self._skills.values(), key=lambda s: s["name"])
        if include_invalid:
            out = out + sorted(self._invalid, key=lambda s: s["name"])
        return [dict(s) for s in out]

    def get_skill(self, name: str) -> dict[str, Any] | None:
        record = self._skills.get(name)
        return dict(record) if record else None

    def exists(self, name: str) -> bool:
        return name in self._skills or any(s["name"] == name for s in self._invalid)

    def skill_path(self, name: str) -> Path:
        record = self._skills.get(name)
        if not record:
            raise SkillResourceError(f"Unknown skill: {name}")
        return Path(record["path"])

    def get_body(self, name: str) -> str:
        text = (self.skill_path(name) / "SKILL.md").read_text(encoding="utf-8")
        try:
            return parse_skill_md(text, expected_name=name).body
        except SkillInvalid as refused:  # changed on disk since the last scan
            raise SkillResourceError(refusal_text(refused, name)) from None

    def list_resources(self, name: str) -> list[str]:
        root = self.skill_path(name)
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )

    def read_resource(self, name: str, relative_path: str) -> str:
        root = self.skill_path(name).resolve()
        rel = Path(relative_path)
        if rel.is_absolute() or ".." in rel.parts or resource_path_refusal(relative_path):
            raise SkillResourceError("Resource paths must be relative, inside the skill folder")
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise SkillResourceError("Resource path escapes the skill folder")
        if not target.is_file():
            raise SkillResourceError(f"No such resource: {relative_path}")
        if target.stat().st_size > RESOURCE_READ_MAX:
            raise SkillResourceError(f"Resource exceeds {RESOURCE_READ_MAX // 1024}KB")
        data = target.read_bytes()
        if b"\x00" in data:
            raise SkillResourceError("Binary resources cannot be read as text")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SkillResourceError("Resource is not valid UTF-8 text") from e


_manager: SkillsManager | None = None


def get_skills_manager() -> SkillsManager:
    """Process-wide manager, constructed from the platform paths on first use."""
    global _manager
    if _manager is None:
        import spark
        from spark.core.application import _get_data_path

        _manager = SkillsManager(
            _get_data_path() / "skills",
            bundled_dir=Path(spark.__file__).parent / "resources" / "skills",
        )
    return _manager


def set_skills_manager(manager: SkillsManager | None) -> None:
    """Inject a manager (server wiring and tests)."""
    global _manager
    _manager = manager
