"""SPARK's skills as a conduit skill source.

SPARK keeps skills on disk in two folders: the bundled skills that ship read-only in
the package, and the instance's own (user) skills, which one administrator manages for
every user of a server. :class:`SparkSkillSource` lists and loads them through conduit's
:class:`~conduit.skills.FilesystemSkillSource`, so conduit's ``SKILL.md`` validation and
its confined resource reads apply to every skill a model can load.

Two rules from SPARK sit on top of conduit's:

* **Uniqueness.** A skill name is unique across the two folders and a bundled skill
  wins: a user skill of the same name is never listed or loaded (the skills page shows
  it as invalid, "name collides with a bundled skill").
* **Per-user scoping.** Each user turns skills on and off for themselves, and per
  conversation (:func:`spark.database.skills.resolve_enabled`). A source is built for
  one user's turn with the names that user has enabled, and lists and loads only those:
  another user's choices never reach it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from conduit.core.backends import Skill, SkillSummary
from conduit.skills import FilesystemSkillSource, SkillNotFound, SkillUnavailable, is_skill_name

__all__ = ["BUNDLED", "USER", "SparkSkillSource"]

logger = logging.getLogger(__name__)

BUNDLED = "bundled"
USER = "user"


def _folder(directory: Path | None) -> FilesystemSkillSource | None:
    """conduit's source over ``directory``, or ``None`` when there is no such folder."""
    if directory is None or not Path(directory).is_dir():
        return None
    return FilesystemSkillSource(directory)


class SparkSkillSource:
    """The bundled and user skills folders as one conduit ``SkillSource``.

    ``enabled`` is the set of names the turn's user has enabled (``None``: every
    skill, for tools and tests that act for no user).
    """

    def __init__(
        self,
        user_dir: Path | None,
        bundled_dir: Path | None = None,
        *,
        enabled: Iterable[str] | None = None,
    ) -> None:
        self._sources: tuple[tuple[str, FilesystemSkillSource], ...] = tuple(
            (origin, source)
            for origin, source in ((BUNDLED, _folder(bundled_dir)), (USER, _folder(user_dir)))
            if source is not None
        )
        self._enabled = frozenset(enabled) if enabled is not None else None

    @property
    def enabled(self) -> frozenset[str] | None:
        return self._enabled

    async def _origins(self) -> dict[str, tuple[str, SkillSummary]]:
        """Every valid skill by name, with the folder it comes from: bundled first, so a
        user skill of a bundled skill's name is left out."""
        found: dict[str, tuple[str, SkillSummary]] = {}
        for origin, source in self._sources:
            try:
                summaries: Sequence[SkillSummary] = await source.list_skills()
            except SkillUnavailable as error:
                logger.warning("The %s skills could not be listed: %s", origin, error.operation)
                continue
            for summary in summaries:
                if summary.name in found:
                    logger.info("Skill %s left out: it collides with a bundled skill", summary.name)
                    continue
                found[summary.name] = (origin, summary)
        return found

    def _allowed(self, name: str) -> bool:
        return self._enabled is None or name in self._enabled

    # The SkillSource protocol.

    async def list_skills(self) -> list[SkillSummary]:
        """The enabled skills' summaries, sorted by name."""
        origins = await self._origins()
        return [origins[name][1] for name in sorted(origins) if self._allowed(name)]

    async def load_skill(self, name: str) -> Skill:
        """An enabled skill, from the folder that owns its name. ``SkillNotFound`` otherwise."""
        if not is_skill_name(name) or not self._allowed(name):
            raise SkillNotFound(name if is_skill_name(name) else None)
        origins = await self._origins()
        if name not in origins:
            raise SkillNotFound(name)
        origin = origins[name][0]
        source = next(source for kind, source in self._sources if kind == origin)
        return await source.load_skill(name)

    async def origin(self, name: str) -> str | None:
        """The folder an enabled skill comes from (``bundled`` or ``user``), if listed."""
        if not self._allowed(name):
            return None
        found = (await self._origins()).get(name)
        return found[0] if found is not None else None
