"""The query_knowledge_graph tool."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_embedder: Any = None

KG_TOOL: dict[str, Any] = {
    "name": "query_knowledge_graph",
    "description": (
        "Query the knowledge graphs available to this conversation for "
        "entities and their relationships. Use when the user asks about "
        "people, projects, systems, or concepts that may have been discussed "
        "before, or asks you to check the knowledge graph."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "entity_name": {
                "type": "string",
                "description": "Optional exact entity name to include in the lookup.",
            },
            "depth": {
                "type": "integer",
                "description": "Relationship hops to expand (1 or 2).",
                "minimum": 1,
                "maximum": 2,
            },
        },
        "required": ["query"],
    },
}


def _get_embedder() -> Any:
    """Module-cached embedding model (shared with the builder)."""
    global _embedder
    if _embedder is None:
        from spark.index.embeddings import EmbeddingModel

        _embedder = EmbeddingModel()
    return _embedder


def get_tools() -> list[dict[str, Any]]:
    return [dict(KG_TOOL)]


def execute(tool_name: str, tool_input: dict, config: dict) -> tuple[str, bool]:
    """Execute query_knowledge_graph. Failure-safe: never an exception."""
    db = config.get("_kg_db")
    user_guid = config.get("_kg_user_guid")
    if db is None or user_guid is None:
        return "The knowledge graph is unavailable in this context.", False
    try:
        from spark.knowledge.query import find_entities, expand, render_subgraph
        from spark.knowledge.resolve import available_scopes

        conversation_id = config.get("_kg_conversation_id")
        scopes = available_scopes(db, conversation_id, user_guid)
        if not scopes:
            return (
                "No knowledge graphs are available here. Graphs can be built "
                "from the Knowledge page or this conversation's settings.",
                False,
            )
        query = str(tool_input.get("query", "")).strip()
        depth = min(2, max(1, int(tool_input.get("depth") or 1)))
        embedder = _get_embedder()
        seeds = find_entities(db, embedder, scopes, query, user_guid)

        entity_name = str(tool_input.get("entity_name", "") or "").strip().lower()
        if entity_name:
            from spark.knowledge import store

            seen = {(s["scope"], s["id"]) for s in seeds}
            for scope in scopes:
                for node in store.get_nodes(db, scope, user_guid):
                    if node["name_key"] == entity_name and (scope, node["id"]) not in seen:
                        seeds.append({**node, "similarity": 1.0, "scope": scope})

        if not seeds:
            return (
                f"No entities matched '{query}' in the available graphs "
                f"({', '.join(scopes)}).",
                False,
            )
        nodes, edges = expand(db, scopes, seeds, user_guid, depth=depth)
        return render_subgraph(nodes, edges), False
    except Exception as e:  # noqa: BLE001 - the tool must never break a turn
        logger.warning("query_knowledge_graph failed: %s", e, exc_info=True)
        return f"Knowledge graph query failed: {e}", True
