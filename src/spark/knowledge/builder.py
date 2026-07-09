"""Background, watermarked, cancellable knowledge graph builds."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from spark.knowledge import store
from spark.knowledge.extractor import chunk_messages, extract_fragment, render_chunk

logger = logging.getLogger(__name__)

NODE_CAP = 5000

_building: set[str] = set()
_building_lock = threading.Lock()


def try_acquire(scope: str) -> bool:
    """Acquire the build lock for a scope. Returns False when already building."""
    with _building_lock:
        if scope in _building:
            return False
        _building.add(scope)
        return True


def release(scope: str) -> None:
    with _building_lock:
        _building.discard(scope)


def is_building(scope: str) -> bool:
    with _building_lock:
        return scope in _building


class KnowledgeGraphBuilder:
    """Builds one scope's graph from its pending sources."""

    def __init__(
        self,
        db: Any,
        service_factory: Callable[[str], Any],
        embedder: Any,
        *,
        status_callback: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._db = db
        self._factory = service_factory
        self._embedder = embedder
        self._emit = status_callback or (lambda t, d: None)

    # ------------------------------------------------------------------ sources

    def _watermarks(self, scope: str, user_guid: str) -> dict[str, int]:
        return {
            row["source_key"]: row["watermark"]
            for row in store.get_build_state(self._db, scope, user_guid)
        }

    def _pending_sources(self, scope: str, user_guid: str) -> list[tuple[str, list[dict]]]:
        """(source_key, pending messages) pairs for a scope."""
        from spark.database import messages as msg_db

        marks = self._watermarks(scope, user_guid)
        sources: list[tuple[str, list[dict]]] = []

        if scope.startswith("conv:"):
            cid = int(scope.split(":", 1)[1])
            conv_ids = [cid]
        else:
            from spark.database import conversations as conv_db

            conv_ids = [c["id"] for c in conv_db.get_active_conversations(self._db, user_guid)]

        for cid in conv_ids:
            key = f"conv:{cid}"
            watermark = marks.get(key, 0)
            msgs = [
                m
                for m in msg_db.get_messages(self._db, cid, include_rolled_up=True)
                if m["id"] > watermark and (m.get("content") or "").strip()
            ]
            if msgs:
                sources.append((key, msgs))

        if scope == "global":
            from spark.database import memories as mem_db

            watermark = marks.get("memories", 0)
            mems = [
                {
                    "id": m["id"],
                    "role": "memory",
                    "content": f"{m.get('category', 'fact')}: {m.get('content', '')}",
                }
                for m in mem_db.get_all_memories(self._db, user_guid, limit=100000)
                if m["id"] > watermark
            ]
            if mems:
                sources.append(("memories", mems))

        return sources

    def plan_scope(self, scope: str, user_guid: str) -> dict[str, int]:
        """Pending work for a scope without any LLM calls (the cost signal)."""
        sources = self._pending_sources(scope, user_guid)
        pending = sum(len(chunk_messages(items)) for _, items in sources)
        return {"sources": len(sources), "pending_chunks": pending}

    # ------------------------------------------------------------------ build

    def build_scope(
        self,
        scope: str,
        user_guid: str,
        model_id: str,
        *,
        cancel_token: Any | None = None,
    ) -> dict[str, Any]:
        """Extract all pending sources for a scope. Synchronous engine."""
        result = {
            "status": "complete",
            "chunks": 0,
            "entities": 0,
            "relationships": 0,
            "skipped_chunks": 0,
            "error": None,
        }

        def cancelled() -> bool:
            return cancel_token is not None and cancel_token.is_cancelled()

        sources = self._pending_sources(scope, user_guid)
        if not sources:
            return result

        service = self._factory(model_id)
        service.set_model(model_id)

        for source_key, items in sources:
            for chunk in chunk_messages(items):
                if cancelled():
                    result["status"] = "cancelled"
                    return result
                if store.count_nodes(self._db, scope, user_guid) >= NODE_CAP:
                    result["status"] = "failed"
                    result["error"] = f"Node cap ({NODE_CAP}) reached for this scope"
                    store.set_watermark(
                        self._db, scope, source_key,
                        self._watermarks(scope, user_guid).get(source_key, 0),
                        user_guid, status="failed", error=result["error"],
                    )
                    return result

                fragment = extract_fragment(service, render_chunk(chunk))
                result["chunks"] += 1
                chunk_max_id = max(m["id"] for m in chunk)

                if fragment is None:
                    result["skipped_chunks"] += 1
                    logger.warning(
                        "KG chunk skipped (no fragment) scope=%s source=%s up to id %d",
                        scope, source_key, chunk_max_id,
                    )
                    continue

                node_ids: dict[str, int] = {}
                for ent in fragment["entities"]:
                    embedding = self._embedder.encode(
                        f"{ent['name']}: {ent['description']}"
                    ).tobytes()
                    node_ids[ent["name"].strip().lower()] = store.upsert_node(
                        self._db, scope, ent["name"], ent["entity_type"],
                        ent["description"], embedding, user_guid,
                    )
                    result["entities"] += 1
                for rel in fragment["relationships"]:
                    src = node_ids.get(rel["source"].strip().lower())
                    dst = node_ids.get(rel["target"].strip().lower())
                    if src is None or dst is None:
                        continue  # relationships must reference this fragment's entities
                    store.upsert_edge(
                        self._db, scope, src, dst, rel["relation"],
                        rel["description"], user_guid,
                    )
                    result["relationships"] += 1

                store.set_watermark(self._db, scope, source_key, chunk_max_id, user_guid)
                self._emit(
                    "kg_progress",
                    {
                        "scope": scope,
                        "source": source_key,
                        "chunks": result["chunks"],
                        "entities": result["entities"],
                    },
                )

        return result
