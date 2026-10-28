"""Knowledge graph endpoints: page, status, build lifecycle, graph JSON."""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spark.core.cancellation import CancellationToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge")

_GRAPH_NODE_CAP = 500


def _db(request: Request) -> Any:
    return request.app.state.conversation_manager._db


def _user_guid(request: Request) -> str:
    return getattr(request.app.state, "user_guid", "default")


def _state_dicts(request: Request) -> tuple[dict, dict]:
    app = request.app
    if not hasattr(app.state, "kg_build_tokens"):
        app.state.kg_build_tokens = {}
    if not hasattr(app.state, "kg_build_results"):
        app.state.kg_build_results = {}
    return app.state.kg_build_tokens, app.state.kg_build_results


@router.get("", response_class=HTMLResponse)
async def knowledge_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "knowledge.html")


@router.get("/api/status")
async def kg_status(request: Request) -> JSONResponse:
    """Per-scope counts, build state, and pending-work plans."""
    from spark.database import conversations as conv_db
    from spark.knowledge import builder, store
    from spark.knowledge.builder import KnowledgeGraphBuilder

    db = _db(request)
    user_guid = _user_guid(request)
    _, results = _state_dicts(request)

    plan_builder = KnowledgeGraphBuilder(db, lambda m: None, None)

    def scope_info(scope: str) -> dict:
        return {
            "nodes": store.count_nodes(db, scope, user_guid),
            "edges": store.count_edges(db, scope, user_guid),
            "building": builder.is_building(scope),
            "builds": store.get_build_state(db, scope, user_guid),
            "last_result": results.get(scope),
            "plan": plan_builder.plan_scope(scope, user_guid),
        }

    scopes = {"global": scope_info("global")}
    ph = db.placeholder
    cur = db.execute(
        f"""SELECT id, name FROM conversations
            WHERE kg_local_enabled = 1 AND is_active = 1 AND user_guid = {ph}""",
        (user_guid,),
    )
    conversation_graphs = []
    for cid, name in cur.fetchall():
        info = scope_info(f"conv:{cid}")
        info.update({"conversation_id": cid, "name": name})
        conversation_graphs.append(info)
    return JSONResponse({"scopes": scopes, "conversation_graphs": conversation_graphs})


@router.post("/api/build")
async def kg_build(request: Request) -> JSONResponse:
    """Start a background build for a scope. 409 when already building."""
    from spark.knowledge import builder
    from spark.knowledge.builder import KnowledgeGraphBuilder
    from spark.knowledge.tools import _get_embedder

    data = await request.json()
    scope = str(data.get("scope", "")).strip()
    if not (scope == "global" or scope.startswith("conv:")):
        return JSONResponse({"error": "Invalid scope"}, status_code=400)

    if not builder.try_acquire(scope):
        return JSONResponse({"error": "A build is already running for this scope"}, status_code=409)

    conv_mgr = request.app.state.conversation_manager
    db = _db(request)
    user_guid = _user_guid(request)
    tokens, results = _state_dicts(request)
    token = CancellationToken()
    tokens[scope] = token

    ctx = getattr(request.app.state, "ctx", None)
    model_id = None
    if ctx is not None:
        try:
            model_id = ctx.settings.get("knowledge_graph.build_model")
        except Exception:  # noqa: BLE001
            model_id = None
    if not model_id:
        active = getattr(conv_mgr, "_llm", None)
        service = getattr(active, "active_service", None) if active else None
        model_id = getattr(service, "current_model", None) or "default"

    def run() -> None:
        try:
            engine = KnowledgeGraphBuilder(db, conv_mgr._get_llm_service_for_model, _get_embedder())
            results[scope] = engine.build_scope(scope, user_guid, model_id, cancel_token=token)
        except Exception as e:  # noqa: BLE001 - record, never crash the thread
            logger.error("Knowledge graph build failed: %s", e, exc_info=True)
            results[scope] = {"status": "failed", "error": str(e)}
        finally:
            builder.release(scope)
            tokens.pop(scope, None)

    threading.Thread(target=run, daemon=True, name=f"kg-build-{scope}").start()
    return JSONResponse({"status": "started", "scope": scope})


@router.post("/api/cancel")
async def kg_cancel(request: Request) -> JSONResponse:
    data = await request.json()
    scope = str(data.get("scope", "")).strip()
    tokens, _ = _state_dicts(request)
    token = tokens.get(scope)
    if token:
        token.cancel("user")
        return JSONResponse({"status": "cancelling"})
    return JSONResponse({"status": "not_building"})


@router.get("/api/graph")
async def kg_graph(request: Request, scope: str) -> JSONResponse:
    """Nodes and edges for the visualisation, capped, without embeddings."""
    from spark.knowledge import store

    db = _db(request)
    user_guid = _user_guid(request)
    total_nodes = store.count_nodes(db, scope, user_guid)
    total_edges = store.count_edges(db, scope, user_guid)
    nodes = store.get_nodes(db, scope, user_guid, limit=_GRAPH_NODE_CAP)
    included = {n["id"] for n in nodes}
    edges = [
        e
        for e in store.get_edges(db, scope, user_guid)
        if e["source_node_id"] in included and e["target_node_id"] in included
    ]
    return JSONResponse(
        {
            "nodes": [{k: v for k, v in n.items() if k != "embedding"} for n in nodes],
            "edges": edges,
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "capped": total_nodes > _GRAPH_NODE_CAP,
        }
    )


@router.get("/api/search")
async def kg_search(request: Request, q: str, scope: str = "global") -> JSONResponse:
    from spark.knowledge.query import find_entities
    from spark.knowledge.tools import _get_embedder

    results = find_entities(_db(request), _get_embedder(), [scope], q, _user_guid(request))
    return JSONResponse(
        {
            "results": [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "entity_type": n["entity_type"],
                    "description": n["description"],
                    "similarity": n["similarity"],
                }
                for n in results
            ]
        }
    )


@router.post("/api/clear")
async def kg_clear(request: Request) -> JSONResponse:
    from spark.knowledge import builder, store

    data = await request.json()
    scope = str(data.get("scope", "")).strip()
    if builder.is_building(scope):
        return JSONResponse({"error": "Cannot clear while building"}, status_code=409)
    store.clear_scope(_db(request), scope, _user_guid(request))
    return JSONResponse({"status": "ok"})
