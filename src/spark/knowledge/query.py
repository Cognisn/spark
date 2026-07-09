"""Subgraph retrieval: embedding match, hop expansion, and compact rendering."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from spark.knowledge import store

if TYPE_CHECKING:
    from spark.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


def find_entities(
    db: DatabaseConnection,
    embedder: Any,
    scopes: list[str],
    query: str,
    user_guid: str,
    *,
    top_k: int = 5,
    threshold: float = 0.35,
) -> list[dict[str, Any]]:
    """Top entities across scopes by cosine similarity to the query."""
    query_vec = np.asarray(embedder.encode(query, normalize=True), dtype=np.float32)
    scored: list[dict[str, Any]] = []
    for scope in scopes:
        for node in store.get_nodes(db, scope, user_guid):
            blob = node.get("embedding")
            if not blob:
                continue
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.shape != query_vec.shape:
                continue
            similarity = float(np.dot(query_vec, vec))
            if similarity >= threshold:
                scored.append({**node, "similarity": similarity, "scope": scope})
    scored.sort(key=lambda n: n["similarity"], reverse=True)
    return scored[:top_k]


def expand(
    db: DatabaseConnection,
    scopes: list[str],
    seed_nodes: list[dict[str, Any]],
    user_guid: str,
    *,
    depth: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """BFS over edges within each seed's own scope, up to depth hops."""
    nodes_by_key: dict[tuple[str, int], dict[str, Any]] = {
        (n["scope"], n["id"]): n for n in seed_nodes
    }
    edges_seen: dict[tuple[str, int], dict[str, Any]] = {}

    frontier = list(nodes_by_key.keys())
    for _ in range(max(0, depth)):
        next_frontier: list[tuple[str, int]] = []
        by_scope: dict[str, list[int]] = {}
        for scope, node_id in frontier:
            by_scope.setdefault(scope, []).append(node_id)
        for scope, node_ids in by_scope.items():
            for edge in store.get_edges_for_nodes(db, scope, node_ids, user_guid):
                edges_seen[(scope, edge["id"])] = {**edge, "scope": scope}
                for endpoint in (edge["source_node_id"], edge["target_node_id"]):
                    key = (scope, endpoint)
                    if key not in nodes_by_key:
                        fetched = store.get_nodes_by_ids(db, scope, [endpoint], user_guid)
                        if fetched:
                            nodes_by_key[key] = {**fetched[0], "scope": scope}
                            next_frontier.append(key)
        frontier = next_frontier
        if not frontier:
            break

    return list(nodes_by_key.values()), list(edges_seen.values())


def render_subgraph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    max_chars: int = 3200,
) -> str:
    """Compact text rendering, strongest weight first, truncated at max_chars."""
    if not nodes:
        return ""
    names = {(n["scope"], n["id"]): n["name"] for n in nodes}
    lines: list[str] = ["Entities:"]
    for node in sorted(nodes, key=lambda n: n.get("weight", 1), reverse=True):
        desc = f": {node['description']}" if node.get("description") else ""
        lines.append(f"- {node['name']} ({node['entity_type']}, {node['scope']}){desc}")
    if edges:
        lines.append("Relationships:")
        for edge in sorted(edges, key=lambda e: e.get("weight", 1), reverse=True):
            src = names.get((edge["scope"], edge["source_node_id"]))
            dst = names.get((edge["scope"], edge["target_node_id"]))
            if src and dst:
                lines.append(f"- {src} —{edge['relation']}→ {dst}")
    out: list[str] = []
    used = 0
    truncated = False
    for line in lines:
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        out.append(line)
        used += len(line) + 1
    if truncated:
        out.append("(truncated)")
    return "\n".join(out)


def subgraph_for_query(
    db: DatabaseConnection,
    embedder: Any,
    scopes: list[str],
    query: str,
    user_guid: str,
    *,
    top_k: int = 5,
    depth: int = 1,
    max_chars: int = 3200,
) -> str:
    """The relevant subgraph for a query, or an empty string when nothing matches."""
    seeds = find_entities(db, embedder, scopes, query, user_guid, top_k=top_k)
    if not seeds:
        return ""
    nodes, edges = expand(db, scopes, seeds, user_guid, depth=depth)
    return render_subgraph(nodes, edges, max_chars=max_chars)
