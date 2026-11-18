"""One chat turn's skills, through conduit's progressive disclosure.

A :class:`TurnSkills` holds a conduit :class:`~conduit.skills.SkillSession` for one
turn of one conversation, over a :class:`~spark.skills.source.SparkSkillSource` that
lists only the skills the turn's user has enabled. The model sees the skills' one-line
descriptions in the system prompt; ``use_skill`` loads a skill, and from then on the
skill's instructions sit in the system prompt (the cached prefix, before its cache
breakpoint) rather than in a tool result. Loading a skill rewrites the prefix once;
every later call of the conversation reads it.

A conversation's loaded skills last beyond the turn: at the start of each turn the
session is restored, in the original order, from the ``use_skill`` calls stored in the
conversation (:func:`spark.connections.transcript_store.skill_transcript`), rolled-up
messages included, so the prefix stays byte-identical across turns and across a
compaction.

SPARK's orchestrators are synchronous and run on worker threads, so the session's
coroutines run on a private event loop the turn owns, one call at a time.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from conduit.core.backends import SkillSummary
from conduit.core.transcript import Transcript
from conduit.core.types import TextBlock
from conduit.skills import DisclosureLimits, LoadedSkill, SkillSession, UseSkillTool

from spark.skills.source import SparkSkillSource

__all__ = ["TASK_CLASS", "TurnSkills", "render_listing", "skill_renderer"]

T = TypeVar("T")

TASK_CLASS = "chat"


def render_listing(summaries: Sequence[SkillSummary]) -> str:
    """The Available Skills block: names and one-line descriptions only."""
    lines = [
        "## Available Skills",
        "When a task matches a skill below, call `use_skill` with its name BEFORE "
        "attempting the task. Its instructions then appear in this system prompt: follow "
        "them. Read a loaded skill's bundled files with `read_skill_resource`.",
    ]
    for summary in summaries:
        version = f" (version {summary.version})" if summary.version else ""
        lines.append(f"- {summary.name}{version}: {summary.description}")
    return "\n".join(lines)


def skill_renderer(folders: Mapping[str, Path]) -> Callable[[LoadedSkill], str]:
    """How a loaded skill appears in the system prompt: its instructions, the folder its
    scripts run from (for ``run_command``), and its resources. Deterministic, so the same
    skills loaded in the same order give the same prefix."""

    def render(loaded: LoadedSkill) -> str:
        version = f' version="{loaded.version}"' if loaded.version else ""
        parts = [f'<skill name="{loaded.name}"{version}>']
        folder = folders.get(loaded.name)
        if folder is not None:
            parts.append(f"Folder (for run_command script paths): {folder}")
            parts.append("")
        parts.append(loaded.body.rstrip("\n"))
        if loaded.resources:
            paths = ", ".join(loaded.resource_paths)
            parts += ["", f"Resources (read with read_skill_resource): {paths}"]
        parts.append("</skill>")
        return "\n".join(parts)

    return render


class TurnSkills:
    """A conversation turn's skill session, called from synchronous code."""

    def __init__(
        self,
        source: SparkSkillSource,
        *,
        folders: Mapping[str, Path] | None = None,
        allowed: Iterable[str] | None = None,
        limits: DisclosureLimits | None = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()
        self.session = SkillSession(
            source,
            limits=limits,
            allowed=allowed,
            task_class=TASK_CLASS,
            render_listing=render_listing,
            render_skill=skill_renderer(folders or {}),
        )
        self._tool = UseSkillTool(self.session, task_classes=[TASK_CLASS])

    def _run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        with self._lock:
            if self._loop.is_closed():
                coroutine.close()
                raise RuntimeError("the turn's skills are closed")
            return self._loop.run_until_complete(coroutine)

    # The conversation's state.

    def restore(self, transcript: Transcript) -> None:
        """Reload the skills the conversation had loaded, in their original order."""
        self._run(self.session.restore(transcript))

    @property
    def loaded(self) -> tuple[str, ...]:
        """The loaded skills' names, in the order they were loaded."""
        return tuple(skill.name for skill in self.session.loaded)

    def listed(self) -> tuple[str, ...]:
        """The skills the model may load (the turn's user's enabled skills)."""
        return tuple(s.name for s in self._run(self.session.summaries()))

    # The request prefix.

    def system(self, base: str) -> str:
        """``base``, then the skill listing, then each loaded skill: the system prompt
        that goes before the cache breakpoint."""
        blocks = self._run(self.session.system([TextBlock(text=base)] if base else []))
        return "\n\n".join(block.text for block in blocks if isinstance(block, TextBlock))

    # The tools.

    def use(self, name: str, resource: str | None = None) -> tuple[str, bool]:
        """``use_skill`` (and ``read_skill_resource`` with ``resource``): the tool's text
        and whether it is an error."""
        arguments: dict[str, Any] = {"name": name}
        if resource is not None:
            arguments["resource"] = resource
        outcome = self._run(self._tool(arguments))
        return outcome.content, not outcome.ok

    def close(self) -> None:
        with self._lock:
            if self._loop.is_closed():
                return
            try:
                self._loop.run_until_complete(self._loop.shutdown_default_executor())
            finally:
                self._loop.close()
