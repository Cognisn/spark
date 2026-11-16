"""Context compaction on conduit's compaction layer.

:class:`ContextCompactor` keeps SPARK's interface (``check_and_compact`` after a turn)
and SPARK's record of a compaction (the older messages rolled up behind a
``[COMPACTED CONTEXT ...]`` message, :func:`spark.database.messages.apply_compaction`),
and leaves the deciding and cutting to conduit's :class:`~conduit.compaction.Compactor`:

* the strategy is chosen by threshold, a share of the model's context window (live:
  :meth:`ContextCompactor.update`);
* the cut keeps the last ``keep_turns`` assistant turns as they are, never splitting a
  tool call from its result, and the summary (a user message) followed by that tail
  keeps the roles alternating;
* while the conversation still fits the window, a compaction is applied only if it
  brings it under the threshold, so a conversation is never compacted again on every
  turn and no summary is paid for that could not help;
* the system prompt, the tool definitions and the loaded skills are never touched
  (they are not part of the stored conversation), so the cached prefix stays
  byte-identical across a compaction.

The summary is SPARK's own: the categorised prompt users know (preserve, compress,
discard), written by the turn's model or the locked compaction model, through the
service the caller gives (billed as ``compaction``). Where a summary cannot be had and
the conversation is past the emergency threshold, conduit's ``KeepLastTurns`` cuts the
older turns and leaves SPARK's truncation notice in their place.

Compaction runs after a turn, on the turn's worker thread; conduit's coroutines run on
a private event loop for the call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from conduit.compaction import (
    CompactionFailed,
    Compactor,
    KeepLastTurns,
    StrategyContext,
    render_messages,
)
from conduit.core.catalogue import ModelEntry
from conduit.core.residency import ResidencyScope
from conduit.core.tasks import ResolvedAssignment, TaskClass
from conduit.core.types import Message, SystemBlock, TextBlock, ToolSpec, Usage

from spark.connections.service import _Names, to_messages
from spark.connections.transcript_store import stored_content

if TYPE_CHECKING:
    from spark.db.bridge import SyncDatabase

__all__ = [
    "COMPACTED_PREFIX",
    "DEFAULT_KEEP_TURNS",
    "TRUNCATED_PREFIX",
    "CompactionSettings",
    "ContextCompactor",
    "ContextLimits",
    "SummaryService",
    "is_compaction_marker",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The assistant turns a compaction keeps as they are (conduit keeps at least one).
DEFAULT_KEEP_TURNS = 1
COMPACTED_PREFIX = "[COMPACTED CONTEXT"
TRUNCATED_PREFIX = "[EMERGENCY TRUNCATION"
TRUNCATION_NOTICE = (
    "[EMERGENCY TRUNCATION — older context was removed to prevent overflow. "
    "Some earlier conversation context has been lost.]"
)
_MIN_SUMMARY_CHARS = 200

_COMPACTION_PROMPT = """Analyse this conversation and produce a structured summary. Categorise each piece of information:

**PRESERVE (full fidelity):**
- Architectural decisions with rationale
- Unresolved issues, bugs, or errors
- Implementation details (code paths, file locations, function names)
- User preferences and constraints
- Critical data and calculations
- Active tasks and pending actions
- Error context and stack traces

**COMPRESS (reduced fidelity):**
- Resolved tasks (brief outcome only)
- Exploratory discussion (conclusions only)
- Tool outputs (key findings only)
- Explanations (final understanding only)

**DISCARD:**
- Redundant information already superseded
- Verbose completions and pleasantries
- Superseded decisions

Structure your output as:
1. **Critical Decisions & Architecture**
2. **Unresolved Issues**
3. **Implementation State**
4. **Key Data & Findings**
5. **User Preferences & Constraints**
6. **Recent Context Summary**
7. **Discarded Topics** (brief list of what was dropped)

CONVERSATION:
{conversation}
"""


class ContextLimits(Protocol):
    """A model's context window and output limit (the connections' catalogue)."""

    def get_context_window(self, model_id: str) -> int: ...

    def get_max_output(self, model_id: str) -> int: ...


class SummaryService(Protocol):
    """What the compactor asks of a model service (:class:`spark.connections.ConnectionService`)."""

    def invoke_model(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = ...,
        temperature: float = ...,
    ) -> dict[str, Any]: ...

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class CompactionSettings:
    """When to compact: shares of the context window, and the summary's size."""

    threshold: float = 0.7
    emergency_threshold: float = 0.95
    summary_ratio: float = 0.3

    def checked(self) -> CompactionSettings:
        """These settings, or the defaults for any value outside (0, 1]."""
        default = CompactionSettings()

        def ratio(value: float, fallback: float) -> float:
            return value if isinstance(value, int | float) and 0 < value <= 1 else fallback

        return CompactionSettings(
            threshold=ratio(self.threshold, default.threshold),
            emergency_threshold=ratio(self.emergency_threshold, default.emergency_threshold),
            summary_ratio=ratio(self.summary_ratio, default.summary_ratio),
        )


def is_compaction_marker(content: Any) -> bool:
    """Whether a stored message is a compaction's summary or truncation notice."""
    return isinstance(content, str) and content.startswith((COMPACTED_PREFIX, TRUNCATED_PREFIX))


@dataclass(frozen=True)
class _Request:
    """What conduit's compaction hook reads (``CompactionContext``)."""

    run_id: str
    task_class: str
    session_id: str | None
    assignment: ResolvedAssignment
    system: tuple[SystemBlock, ...]
    tools: tuple[ToolSpec, ...]
    messages: tuple[Message, ...]
    last_usage: Usage | None


def _assignment(model_id: str, window: int, max_output: int) -> ResolvedAssignment:
    """The conversation's model as conduit's compactor sees it: its window and output."""
    output = max(1, min(max_output, window))
    entry = ModelEntry(
        model_id=model_id or "unknown",
        provider="spark",
        residency_scope=ResidencyScope.GLOBAL,
        context_window=max(1, window),
        output_limit=output,
    )
    return ResolvedAssignment(
        task_class=TaskClass(name="compaction"),
        provider="spark",
        kind="spark",
        model=model_id or "unknown",
        entry=entry,
        max_output_tokens=output,
        provider_region=None,
    )


class _Summary:
    """SPARK's summary as a conduit compaction strategy."""

    name = "summarise"

    def __init__(
        self,
        service: SummaryService,
        *,
        threshold: float,
        keep_turns: int,
        max_output: int,
        status: Callable[[str, dict[str, Any]], Any] | None,
    ) -> None:
        self.threshold = threshold
        self.keep_turns = keep_turns
        self.summary_allowance = max_output
        self._service = service
        self._status = status
        self.text: str | None = None

    async def summarise(self, replaced: Sequence[Message], context: StrategyContext) -> str:
        prompt = _COMPACTION_PROMPT.format(conversation=render_messages(replaced))
        if self._status is not None:
            self._status("compaction_progress", {"stage": "invoking_model"})
        # Low temperature for a focused summary. The service is synchronous and this
        # loop is the compaction's own, so the call is made here.
        result = self._service.invoke_model(
            [{"role": "user", "content": prompt}],
            max_tokens=self.summary_allowance,
            temperature=0.2,
        )
        content = str(result.get("content", "") or "").strip()
        if len(content) < _MIN_SUMMARY_CHARS:
            logger.warning("Compaction output too short (%d chars), skipping", len(content))
            raise CompactionFailed("summary_empty")
        self.text = content
        return content


def _run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run one of conduit's coroutines on a private loop of the calling (worker) thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


@dataclass(frozen=True)
class _Conversation:
    """A conversation's active stored messages, as conduit messages."""

    rows: tuple[Mapping[str, Any], ...]
    messages: tuple[Message, ...]
    positions: tuple[int, ...]  # the row behind each message

    @classmethod
    def of(cls, rows: Sequence[Mapping[str, Any]]) -> _Conversation:
        ordered = ordered_for_model(rows)
        names = _Names()
        messages: list[Message] = []
        positions: list[int] = []
        for position, row in enumerate(ordered):
            converted = to_messages(
                [{"role": row.get("role"), "content": stored_content(row.get("content", ""))}],
                names,
            )
            for message in converted:
                messages.append(message)
                positions.append(position)
        return cls(tuple(ordered), tuple(messages), tuple(positions))

    def rolled_up(self, kept: int, protected: set[int]) -> list[Mapping[str, Any]]:
        """The rows before the kept tail of ``kept`` messages (protected rows stay)."""
        if kept >= len(self.messages):
            return []
        first_kept = self.positions[len(self.messages) - kept] if kept else len(self.rows)
        return [row for row in self.rows[:first_kept] if row["id"] not in protected]


def ordered_for_model(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Active messages in the order the model is sent them: the latest compaction's
    summary first, then every other message in stored order.

    A compaction keeps its last turns as they are, but its summary is stored after
    them (it is written last); the model must read the summary first.
    """
    markers = [i for i, row in enumerate(rows) if is_compaction_marker(row.get("content"))]
    if not markers:
        return list(rows)
    latest = markers[-1]
    return [rows[latest], *(row for i, row in enumerate(rows) if i != latest)]


class ContextCompactor:
    """Compacts a conversation after a turn, with conduit's compactor."""

    def __init__(
        self,
        llm_service: SummaryService | None,
        db: SyncDatabase,
        context_limits: ContextLimits,
        *,
        threshold: float = 0.7,
        emergency_threshold: float = 0.95,
        summary_ratio: float = 0.3,
        locked_model: str | None = None,
        keep_turns: int = DEFAULT_KEEP_TURNS,
    ) -> None:
        self._llm = llm_service
        self._db = db
        self._context_limits = context_limits
        self._settings = CompactionSettings(threshold, emergency_threshold, summary_ratio)
        self._locked_model = locked_model
        self._keep_turns = max(1, keep_turns)

    @property
    def locked_model(self) -> str | None:
        """The model every compaction uses, when one is locked (the caller resolves its service)."""
        return self._locked_model

    @property
    def settings(self) -> CompactionSettings:
        return self._settings

    def update(self, settings: CompactionSettings) -> None:
        """Use ``settings`` from the next compaction check on (a settings change)."""
        self._settings = settings.checked()

    def check_and_compact(
        self,
        conversation_id: int,
        model_id: str,
        user_guid: str,
        *,
        in_tool_use_loop: bool = False,
        status_callback: Any | None = None,
        service: SummaryService | None = None,
    ) -> bool:
        """Check if compaction is needed and perform it. Returns True if compacted.

        ``service`` is the one to summarise with and count tokens through (a turn's
        own); without it the compactor's own applies. One compactor serves every
        turn, so the service is passed down each call, never kept on the instance.
        """
        service = service or self._llm
        if service is None:
            return False
        from spark.database import conversations

        conv = self._db.call(conversations.get_conversation, conversation_id, user_guid)
        if not conv:
            logger.warning(
                "Compaction check skipped: conversation %d not found for user_guid=%r",
                conversation_id,
                user_guid,
            )
            return False

        settings = self._settings
        total_tokens = int(conv.get("total_tokens", 0) or 0)
        window = self._context_limits.get_context_window(model_id)
        normal = int(window * settings.threshold)
        emergency = int(window * settings.emergency_threshold)
        logger.debug(
            "Compaction check: conv=%d tokens=%d threshold=%d emergency=%d model=%s",
            conversation_id,
            total_tokens,
            normal,
            emergency,
            model_id,
        )
        if total_tokens < normal:
            return False
        # Defer during tool use unless emergency
        if in_tool_use_loop and total_tokens < emergency:
            logger.info(
                "Deferring compaction during tool use (tokens=%d, emergency=%d)",
                total_tokens,
                emergency,
            )
            return False
        if total_tokens >= emergency:
            logger.warning("Emergency compaction triggered (tokens=%d)", total_tokens)
        else:
            logger.info("Compaction triggered (tokens=%d, threshold=%d)", total_tokens, normal)
        if status_callback:
            status_callback("compaction_start", {"tokens": total_tokens, "threshold": normal})

        try:
            done = self._compact(
                service, conversation_id, model_id, user_guid, total_tokens, status_callback
            )
        except Exception as e:  # noqa: BLE001 - a failed compaction must never fail the turn
            logger.error("Compaction failed: %s", e)
            done = False
        if not done and total_tokens >= emergency:
            return self._truncate(service, conversation_id, model_id, user_guid, total_tokens)
        return done

    # -- the compaction ---------------------------------------------------------------

    def _load(self, conversation_id: int, user_guid: str) -> _Conversation:
        from spark.database import messages

        rows = self._db.call(
            messages.get_messages, user_guid, conversation_id, include_rolled_up=False
        )
        return _Conversation.of(rows)

    def _request(
        self, conversation_id: int, model_id: str, conversation: _Conversation, tokens: int
    ) -> _Request:
        window = self._context_limits.get_context_window(model_id)
        max_output = self._context_limits.get_max_output(model_id)
        return _Request(
            run_id=f"compaction-{conversation_id}",
            task_class="compaction",
            session_id=None,
            assignment=_assignment(model_id, window, max_output),
            system=(),
            tools=(),
            messages=conversation.messages,
            # SPARK's count of the conversation, when larger than conduit's estimate
            last_usage=Usage(
                input_tokens=max(0, tokens),
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
        )

    def _compact(
        self,
        service: SummaryService,
        conversation_id: int,
        model_id: str,
        user_guid: str,
        total_tokens: int,
        status_callback: Any | None,
    ) -> bool:
        """Summarise the older turns with conduit's compactor and SPARK's summary."""
        conversation = self._load(conversation_id, user_guid)
        if len(conversation.messages) < 4:
            return False
        settings = self._settings
        original_tokens = sum(int(row.get("token_count", 0) or 0) for row in conversation.rows)
        model_max = self._context_limits.get_max_output(model_id)
        target = max(2000, int(original_tokens * settings.summary_ratio))
        strategy = _Summary(
            service,
            threshold=settings.threshold,
            keep_turns=self._keep_turns,
            max_output=max(1, min(model_max, target, 16000)),
            status=status_callback,
        )
        compactor = Compactor([strategy], cache_summary=False)
        request = self._request(conversation_id, model_id, conversation, total_tokens)
        replacement = _run(compactor.before_model_call(request))
        if replacement is None or strategy.text is None:
            return False
        return self._store(
            service,
            conversation_id,
            user_guid,
            conversation,
            kept=len(replacement) - 1,
            marker=f"[COMPACTED CONTEXT — {datetime.now(timezone.utc).isoformat()}]\n\n"
            f"{strategy.text}",
            summary=strategy.text,
            status_callback=status_callback,
        )

    def _truncate(
        self,
        service: SummaryService,
        conversation_id: int,
        model_id: str,
        user_guid: str,
        total_tokens: int,
    ) -> bool:
        """Last resort: conduit's KeepLastTurns, with SPARK's truncation notice."""
        try:
            conversation = self._load(conversation_id, user_guid)
            strategy = KeepLastTurns(
                threshold=self._settings.emergency_threshold,
                keep_turns=self._keep_turns,
                notice=TRUNCATION_NOTICE,
            )
            compactor = Compactor([strategy], cache_summary=False)
            compactor.force(None)
            request = self._request(conversation_id, model_id, conversation, total_tokens)
            replacement = _run(compactor.before_model_call(request))
        except Exception as e:  # noqa: BLE001 - truncation must never fail the turn
            logger.error("Emergency truncation failed: %s", e)
            return False
        if replacement is None:
            return False
        done = self._store(
            service,
            conversation_id,
            user_guid,
            conversation,
            kept=len(replacement) - 1,
            marker=TRUNCATION_NOTICE,
            summary=None,
            status_callback=None,
        )
        if done:
            logger.warning("Emergency truncation: older messages removed")
        return done

    def _store(
        self,
        service: SummaryService,
        conversation_id: int,
        user_guid: str,
        conversation: _Conversation,
        *,
        kept: int,
        marker: str,
        summary: str | None,
        status_callback: Any | None,
    ) -> bool:
        """Roll up the rows before the kept tail behind ``marker``, in one transaction."""
        from spark.database import messages

        protected = _find_in_flight_tool_messages(
            [
                {"id": row["id"], "content": stored_content(row.get("content", ""))}
                for row in conversation.rows
            ]
        )
        rolled = conversation.rolled_up(kept, protected)
        rollup_ids = [int(row["id"]) for row in rolled]
        if not rollup_ids:
            return False
        original_tokens = sum(int(row.get("token_count", 0) or 0) for row in rolled)
        new_total = self._db.call(
            messages.apply_compaction,
            conversation_id,
            marker,
            service.count_tokens(summary if summary is not None else marker),
            rollup_ids,
            user_guid,
            summary=summary,
            original_tokens=original_tokens if summary is not None else None,
        )
        logger.info(
            "Compaction complete: %d→%d tokens (%d messages rolled up)",
            original_tokens,
            new_total,
            len(rollup_ids),
        )
        if status_callback:
            status_callback(
                "compaction_complete",
                {
                    "original_tokens": original_tokens,
                    "new_tokens": new_total,
                    "messages_rolled_up": len(rollup_ids),
                },
            )
        return True


def _format_messages(msgs: list[dict[str, Any]]) -> str:
    """Format messages into readable text for the compaction prompt (conduit's rendering)."""
    names = _Names()
    return render_messages(
        [m for msg in msgs for m in to_messages([msg], names)],
    )


def _find_in_flight_tool_messages(msgs: list[dict[str, Any]]) -> set[int]:
    """Find message IDs containing tool_use blocks without matching tool_results."""
    # Collect all completed tool_use_ids
    completed: set[str] = set()
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    completed.add(block.get("tool_use_id", ""))

    # Find messages with unmatched tool_use blocks
    protected: set[int] = set()
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("id", "") not in completed:
                        protected.add(msg["id"])
                        break

    return protected
