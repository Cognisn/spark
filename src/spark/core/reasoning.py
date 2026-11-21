"""A model's reasoning, kept apart from its answer.

conduit delivers a model's reasoning as provider state blocks, not as text:
Claude's ``thinking`` blocks (Anthropic API and Bedrock ``reasoning_content``) and
Amazon Nova's ``thinking_text``. SPARK's response format carries them in
``content_blocks`` as ``provider_state`` entries with a base64 payload. This module
reads the readable text out of those blocks so it can be shown beside the answer,
and strips the blocks from text that leaves the chat (an export).

The blocks themselves stay in a stored tool-use message, because conduit needs
Claude's reasoning sent back with its tool calls; conduit decides what a provider
receives (``thinking_text`` is never sent back to any model).
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["block_reasoning", "response_reasoning", "strip_reasoning_blocks"]


def _is_state(block: Any) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "provider_state"


def block_reasoning(block: Any) -> str:
    """The readable reasoning text of one content block, or ``""``.

    Only a ``provider_state`` block holding reasoning text counts. Redacted
    reasoning, signatures and any state that is not text yield nothing.
    """
    if not _is_state(block):
        return ""
    try:
        raw = base64.b64decode(str(block.get("payload", "")), validate=True)
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, RecursionError):
        return ""
    if not isinstance(data, Mapping):
        return ""
    text: Any = data.get("thinking")
    if text is None:
        inner = data.get("reasoningText")
        text = inner.get("text") if isinstance(inner, Mapping) else None
    return text.strip() if isinstance(text, str) else ""


def response_reasoning(blocks: Iterable[Any] | None) -> str:
    """The reasoning of one model response, its blocks' texts in order, blank-line separated."""
    parts = [text for text in (block_reasoning(b) for b in blocks or ()) if text]
    return "\n\n".join(parts)


def strip_reasoning_blocks(content: str) -> str:
    """``content`` without reasoning, for text that leaves the chat.

    A stored tool-use message is a JSON list of blocks; its ``provider_state``
    blocks (the reasoning and its signatures) are removed. Any other content is
    returned unchanged.
    """
    if not content.startswith("["):
        return content
    try:
        blocks = json.loads(content)
    except ValueError:
        return content
    if not isinstance(blocks, list) or not any(_is_state(b) for b in blocks):
        return content
    return json.dumps([b for b in blocks if not _is_state(b)])
