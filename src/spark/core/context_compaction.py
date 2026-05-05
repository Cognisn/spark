"""LLM-driven intelligent context compaction."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection
    from spark.llm.base import LLMService
    from spark.llm.context_limits import ContextLimitResolver

logger = logging.getLogger(__name__)

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


class ContextCompactor:
    """Manages intelligent context compaction using LLM-driven summarisation."""

    def __init__(
        self,
        llm_service: LLMService,
        db: DatabaseConnection,
        context_limits: ContextLimitResolver,
        *,
        threshold: float = 0.7,
        emergency_threshold: float = 0.95,
        summary_ratio: float = 0.3,
        locked_model: str | None = None,
    ) -> None:
        self._llm = llm_service
        self._db = db
        self._context_limits = context_limits
        self._threshold = threshold
        self._emergency_threshold = emergency_threshold
        self._summary_ratio = summary_ratio
        self._locked_model = locked_model

    def check_and_compact(
        self,
        conversation_id: int,
        model_id: str,
        user_guid: str,
        *,
        in_tool_use_loop: bool = False,
        status_callback: Any | None = None,
    ) -> bool:
        """Check if compaction is needed and perform it. Returns True if compacted."""
        from spark.database import conversations

        conv = conversations.get_conversation(self._db, conversation_id, user_guid)
        if not conv:
            logger.warning(
                "Compaction check skipped: conversation %d not found for user_guid=%r",
                conversation_id,
                user_guid,
            )
            return False

        total_tokens = conv.get("total_tokens", 0)
        context_window = self._context_limits.get_context_window(model_id)
        normal_threshold = int(context_window * self._threshold)
        emergency = int(context_window * self._emergency_threshold)

        logger.debug(
            "Compaction check: conv=%d tokens=%d threshold=%d emergency=%d model=%s",
            conversation_id,
            total_tokens,
            normal_threshold,
            emergency,
            model_id,
        )

        if total_tokens < normal_threshold:
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
            logger.info(
                "Compaction triggered (tokens=%d, threshold=%d)",
                total_tokens,
                normal_threshold,
            )

        if status_callback:
            status_callback(
                "compaction_start", {"tokens": total_tokens, "threshold": normal_threshold}
            )

        try:
            return self._perform_compaction(
                conversation_id, model_id, user_guid, status_callback
            )
        except Exception as e:
            logger.error("Compaction failed: %s", e)
            if total_tokens >= emergency:
                return self._emergency_truncation(conversation_id, model_id, user_guid)
            return False

    def _perform_compaction(
        self,
        conversation_id: int,
        model_id: str,
        user_guid: str,
        status_callback: Any | None = None,
    ) -> bool:
        """Execute LLM-driven context compaction."""
        from spark.database import conversations, messages

        all_msgs = messages.get_messages(self._db, conversation_id, include_rolled_up=False)
        if len(all_msgs) < 4:
            return False

        formatted = _format_messages(all_msgs)
        original_tokens = sum(m.get("token_count", 0) for m in all_msgs)

        # Calculate max output tokens
        model_max = self._context_limits.get_max_output(model_id)
        target = max(2000, int(original_tokens * self._summary_ratio))
        max_output = min(model_max, target, 16000)

        prompt = _COMPACTION_PROMPT.format(conversation=formatted)

        if status_callback:
            status_callback("compaction_progress", {"stage": "invoking_model"})

        # Invoke with low temperature for focused output
        effective_model = self._locked_model or model_id
        result = self._llm.invoke_model(
            [{"role": "user", "content": prompt}],
            max_tokens=max_output,
            temperature=0.2,
        )

        content = result.get("content", "").strip()
        if len(content) < 200:
            logger.warning("Compaction output too short (%d chars), skipping", len(content))
            return False

        # Store compacted content
        return self._store_compaction(
            conversation_id,
            all_msgs,
            content,
            original_tokens,
            user_guid,
            status_callback,
        )

    def _store_compaction(
        self,
        conversation_id: int,
        original_messages: list[dict],
        compacted_content: str,
        original_tokens: int,
        user_guid: str,
        status_callback: Any | None = None,
    ) -> bool:
        """Store compaction results and mark originals as rolled up."""
        from spark.database import conversations, messages

        now = datetime.now(timezone.utc).isoformat()
        marker = f"[COMPACTED CONTEXT — {now}]\n\n{compacted_content}"

        # Find in-flight tool_use messages to protect
        protected_ids = _find_in_flight_tool_messages(original_messages)

        # IDs to roll up (excluding protected)
        rollup_ids = [m["id"] for m in original_messages if m["id"] not in protected_ids]

        if not rollup_ids:
            return False

        # Add compacted content as a new message
        compacted_tokens = self._llm.count_tokens(compacted_content)
        messages.add_message(
            self._db, conversation_id, "user", marker, compacted_tokens, user_guid
        )

        # Mark originals
        messages.mark_messages_as_rolled_up(self._db, conversation_id, rollup_ids)

        # Record rollup history
        messages.record_rollup(
            self._db,
            conversation_id,
            len(rollup_ids),
            compacted_content,
            original_tokens,
            compacted_tokens,
            user_guid,
        )

        # Recalculate
        new_total = conversations.recalculate_total_tokens(self._db, conversation_id)

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

    def _emergency_truncation(
        self, conversation_id: int, model_id: str, user_guid: str
    ) -> bool:
        """Last resort: keep only recent messages."""
        from spark.database import conversations, messages

        context_window = self._context_limits.get_context_window(model_id)
        target = int(context_window * 0.2)

        all_msgs = messages.get_messages(self._db, conversation_id, include_rolled_up=False)
        if not all_msgs:
            return False

        # Keep most recent messages up to target tokens
        keep_tokens = 0
        keep_from = len(all_msgs)
        for i in range(len(all_msgs) - 1, -1, -1):
            keep_tokens += all_msgs[i].get("token_count", 0)
            if keep_tokens > target:
                break
            keep_from = i

        rollup_ids = [m["id"] for m in all_msgs[:keep_from]]
        if not rollup_ids:
            return False

        truncation_notice = (
            "[EMERGENCY TRUNCATION — older context was removed to prevent overflow. "
            "Some earlier conversation context has been lost.]"
        )
        messages.add_message(
            self._db,
            conversation_id,
            "user",
            truncation_notice,
            self._llm.count_tokens(truncation_notice),
            user_guid,
        )
        messages.mark_messages_as_rolled_up(self._db, conversation_id, rollup_ids)
        conversations.recalculate_total_tokens(self._db, conversation_id)

        logger.warning("Emergency truncation: removed %d messages", len(rollup_ids))
        return True


def _format_messages(msgs: list[dict]) -> str:
    """Format messages into readable text for compaction prompt."""
    lines: list[str] = []
    for msg in msgs:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")

        if isinstance(content, str):
            # Truncate very long messages
            if len(content) > 3000:
                content = content[:2900] + "\n... [truncated]"
            lines.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        parts.append(f"[Tool call: {block.get('name', '?')}]")
                    elif block.get("type") == "tool_result":
                        result = str(block.get("content", ""))[:500]
                        parts.append(f"[Tool result: {result}]")
            text = "\n".join(parts)
            if len(text) > 3000:
                text = text[:2900] + "\n... [truncated]"
            lines.append(f"[{role}]: {text}")

    return "\n\n".join(lines)


def _find_in_flight_tool_messages(msgs: list[dict]) -> set[int]:
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
