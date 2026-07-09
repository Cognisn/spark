"""LLM extraction of knowledge graph fragments from message batches."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ENTITY_TYPES = frozenset(
    {"person", "organisation", "project", "system", "concept", "place", "event", "other"}
)

RECORD_FRAGMENT_TOOL: dict[str, Any] = {
    "name": "record_graph_fragment",
    "description": (
        "Record the entities and relationships found in the supplied text. "
        "Always call this exactly once with everything you found."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "entity_type": {
                            "type": "string",
                            "enum": sorted(ENTITY_TYPES),
                        },
                        "description": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
            "relationships": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "relation": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["source", "target", "relation"],
                },
            },
        },
        "required": ["entities", "relationships"],
    },
}

_EXTRACTION_SYSTEM = (
    "You are a knowledge graph extractor. From the supplied conversation text, "
    "extract real-world entities (people, organisations, projects, systems, "
    "concepts, places, events) and the typed relationships between them that "
    "the text explicitly supports. Keep descriptions to one concise sentence. "
    "Do not speculate or invent relationships. Ignore conversational filler. "
    "ALWAYS respond by calling `record_graph_fragment` exactly once, even when "
    "you found nothing (pass empty lists)."
)

_RETRY_PREFIX = (
    "[You did not call record_graph_fragment last time. You MUST respond only "
    "by calling the record_graph_fragment tool.]\n\n"
)


def chunk_messages(
    messages: list[dict[str, Any]], *, chunk_chars: int = 12000
) -> list[list[dict[str, Any]]]:
    """Greedily batch messages into chunks of at most chunk_chars of content."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for msg in messages:
        content = msg.get("content") or ""
        if len(content) > chunk_chars:
            msg = {**msg, "content": content[:chunk_chars]}
            content = msg["content"]
        if current and size + len(content) > chunk_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(msg)
        size += len(content)
    if current:
        chunks.append(current)
    return chunks


def render_chunk(messages: list[dict[str, Any]]) -> str:
    """Render messages as 'ROLE: content' lines for the extraction prompt."""
    return "\n".join(
        f"{(m.get('role') or 'unknown').upper()}: {m.get('content') or ''}" for m in messages
    )


def _normalise(raw: dict[str, Any]) -> dict[str, Any] | None:
    entities = []
    for ent in raw.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        name = str(ent.get("name", "")).strip()
        if not name:
            continue
        entity_type = str(ent.get("entity_type", "other")).strip().lower()
        if entity_type not in ENTITY_TYPES:
            entity_type = "other"
        entities.append(
            {
                "name": name,
                "entity_type": entity_type,
                "description": str(ent.get("description", "") or "").strip(),
            }
        )
    relationships = []
    for rel in raw.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        source = str(rel.get("source", "")).strip()
        target = str(rel.get("target", "")).strip()
        relation = str(rel.get("relation", "")).strip()
        if not (source and target and relation):
            continue
        relationships.append(
            {
                "source": source,
                "target": target,
                "relation": relation,
                "description": str(rel.get("description", "") or "").strip(),
            }
        )
    if not entities and not relationships:
        return None
    return {"entities": entities, "relationships": relationships}


def extract_fragment(service: Any, chunk_text: str) -> dict[str, Any] | None:
    """Extract one fragment from a chunk. One stricter retry, then None."""
    for attempt, text in enumerate((chunk_text, _RETRY_PREFIX + chunk_text)):
        try:
            response = service.invoke_model(
                [{"role": "user", "content": text}],
                max_tokens=4096,
                temperature=0.2,
                tools=[RECORD_FRAGMENT_TOOL],
                system=_EXTRACTION_SYSTEM,
            )
        except Exception as e:  # noqa: BLE001 - the caller records skipped chunks
            logger.warning("Extraction call failed (attempt %d): %s", attempt + 1, e)
            continue
        for tc in response.get("tool_use") or []:
            if tc.get("name") == "record_graph_fragment":
                return _normalise(tc.get("input") or {})
    return None
