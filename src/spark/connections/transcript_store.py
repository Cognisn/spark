"""conduit transcripts read from SPARK's stored conversations.

SPARK keeps its own transcript: the ``messages`` table, in SPARK's format, with
compaction recorded by rolling messages up behind a summary message
(:func:`spark.database.messages.apply_compaction`). That stays the record. conduit's
skills layer needs only one thing from it: which skills a conversation has loaded, in
order, so a new turn's :class:`~conduit.skills.SkillSession` can be restored and its
cached prefix stays byte-identical (``SkillSession.restore``).

:func:`skill_transcript` builds that conduit :class:`~conduit.core.transcript.Transcript`
from the stored messages: a ``SkillUse`` marker for each ``use_skill`` call whose result
was not an error, in the order the calls were made. Rolled-up messages are read too, so
skills loaded before a compaction stay loaded after it. Nothing here writes: no
conduit layer SPARK uses appends to a transcript (the compactor runs without a store,
and SPARK persists the compaction itself), so SPARK needs no second copy of its
conversations.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from conduit.core.transcript import SkillUse, Transcript
from conduit.skills import is_skill_name

__all__ = ["TOOL_RESULTS_MARKER", "skill_transcript", "skill_uses", "stored_content"]

#: The prefix of a stored user message that holds a turn's tool results.
TOOL_RESULTS_MARKER = "[TOOL_RESULTS]"

_LOADS = frozenset({"use_skill"})


def stored_content(content: Any) -> Any:
    """A stored message's content as SPARK sends it: its JSON blocks when it holds
    blocks (tool calls, tool results), otherwise the text as stored."""
    if not isinstance(content, str):
        return content
    text = content[len(TOOL_RESULTS_MARKER) :] if content.startswith(TOOL_RESULTS_MARKER) else None
    if text is None and content.startswith("["):
        text = content
    if text is not None:
        try:
            blocks = json.loads(text)
        except json.JSONDecodeError:
            return content
        if isinstance(blocks, list):
            return blocks
    return content


def _blocks(content: Any) -> list[Mapping[str, Any]]:
    parsed = stored_content(content)
    if not isinstance(parsed, list):
        return []
    return [block for block in parsed if isinstance(block, Mapping)]


def _loaded_name(block: Mapping[str, Any]) -> str | None:
    """The skill a ``use_skill`` call loads (not a resource read), if it names a valid one."""
    if block.get("type") != "tool_use" or block.get("name") not in _LOADS:
        return None
    arguments = block.get("input")
    if not isinstance(arguments, Mapping) or arguments.get("resource"):
        return None
    name = arguments.get("skill_name", arguments.get("name"))
    return name if isinstance(name, str) and is_skill_name(name) else None


def skill_uses(rows: Iterable[Mapping[str, Any]]) -> list[SkillUse]:
    """A ``loaded`` marker for each successful ``use_skill`` call in ``rows`` (stored
    messages, oldest first)."""
    pending: dict[str, str] = {}
    markers: list[SkillUse] = []
    for row in rows:
        for block in _blocks(row.get("content")):
            name = _loaded_name(block)
            if name is not None:
                pending[str(block.get("id", ""))] = name
            elif block.get("type") == "tool_result":
                loaded = pending.pop(str(block.get("tool_use_id", "")), None)
                if loaded is not None and not block.get("is_error"):
                    markers.append(SkillUse(skill=loaded, action="loaded"))
    return markers


def skill_transcript(session_id: str, rows: Iterable[Mapping[str, Any]]) -> Transcript:
    """The conversation's skill-use markers as a conduit transcript, numbered in order."""
    entries = tuple(
        marker.model_copy(update={"seq": seq})
        for seq, marker in enumerate(skill_uses(rows), start=1)
    )
    return Transcript(session_id=session_id, entries=entries)
