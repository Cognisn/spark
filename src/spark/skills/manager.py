"""Skill discovery, validation, and jailed resource access.

Skills are folders containing SKILL.md (YAML frontmatter: name, description)
plus optional resource files. Bundled skills ship read-only in the package;
user skills live in the platform data directory. Content truth is on disk.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_MD_MAX = 64 * 1024
RESOURCE_READ_MAX = 256 * 1024
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class SkillValidationError(Exception):
    """Raised when authored skill input is invalid."""


class SkillResourceError(Exception):
    """Raised on resource jail, size, or type violations."""


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
        record["error"] = f"SKILL.md unreadable: {e}"
        return record
    match = _FRONTMATTER_RE.match(text)
    if not match:
        record["error"] = "missing YAML frontmatter"
        return record
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as e:
        record["error"] = f"frontmatter is not valid YAML: {e}"
        return record
    name = str(meta.get("name", "")).strip()
    description = str(meta.get("description", "") or "").strip()
    if not NAME_RE.match(name) or len(name) > 64:
        record["error"] = "frontmatter name must be kebab-case, max 64 chars"
        return record
    if name != path.name:
        record["error"] = f"frontmatter name '{name}' does not match folder '{path.name}'"
        return record
    if not description or len(description) > 1024:
        record["error"] = "description is required (1 to 1024 characters)"
        return record
    record.update({"name": name, "description": description, "valid": True})
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
        return _FRONTMATTER_RE.sub("", text, count=1)

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
        if rel.is_absolute() or ".." in rel.parts:
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
