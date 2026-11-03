"""Knowledge graph endpoints: page, status, build lifecycle, graph JSON.

Personal mode builds in the web process, on a thread, as it always has. In
server mode no build runs in a web process: a build request queues a job
(:mod:`spark.knowledge.jobs`) that the worker claims and builds as its owner
(:mod:`spark.knowledge.build_jobs`), and status, progress, cancel and retry all
go through the job table, so any web replica answers for any build. On a
server the global graph needs ``spark.knowledge.instance`` to build, retry or
clear.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spark.core.cancellation import CancellationToken
from spark.identity.access import current_user_guid, has_permission
from spark.identity.permissions import KNOWLEDGE_INSTANCE
from spark.knowledge import jobs
from spark.web.db import database, sync_database
from spark.web.scope import is_server, not_found, owned_conversation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge")

_GRAPH_NODE_CAP = 500


def build_key(user_guid: str, scope: str) -> str:
    """One user's build of one scope: its lock, cancel token and last result."""
    return f"{user_guid}|{scope}"


async def _scope_refusal(request: Request, scope: str) -> JSONResponse | None:
    """400 for a malformed scope, 404 for another user's (or a missing) conversation."""
    if scope == "global":
        return None
    kind, _, raw = scope.partition(":")
    if kind != "conv" or not raw.isdigit():
        return JSONResponse({"error": "Invalid scope"}, status_code=400)
    if await owned_conversation(request, int(raw)) is None:
        return not_found()
    return None


def _permission_refusal(request: Request, scope: str) -> JSONResponse | None:
    """403 on a server when changing ``scope`` needs ``spark.knowledge.instance`` the user lacks."""
    if not is_server() or not jobs.needs_instance_permission(scope):
        return None
    if has_permission(request, KNOWLEDGE_INSTANCE):
        return None
    return JSONResponse(
        {"error": "Building the global graph needs the instance knowledge graph permission"},
        status_code=403,
    )


def _state_dicts(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    app = request.app
    if not hasattr(app.state, "kg_build_tokens"):
        app.state.kg_build_tokens = {}
    if not hasattr(app.state, "kg_build_results"):
        app.state.kg_build_results = {}
    return app.state.kg_build_tokens, app.state.kg_build_results


@router.get("", response_class=HTMLResponse)
async def knowledge_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    page: HTMLResponse = templates.TemplateResponse(request, "knowledge.html")
    return page


@router.get("/api/status")
async def kg_status(request: Request) -> JSONResponse:
    """Per-scope counts, build state, and pending-work plans."""
    from spark.knowledge import builder, store
    from spark.knowledge.builder import KnowledgeGraphBuilder

    db = database(request)
    user_guid = current_user_guid(request)
    server = is_server()
    _, results = _state_dicts(request)
    plan_builder = KnowledgeGraphBuilder(sync_database(request), lambda m: None, None)

    async def build_state(scope: str) -> dict[str, Any]:
        if not server:
            return {
                "building": builder.is_building(build_key(user_guid, scope)),
                "last_result": results.get(build_key(user_guid, scope)),
            }
        # Any replica: the job table holds the state of every build.
        latest = await jobs.latest(db, user_guid, scope)
        finished = (
            latest
            if latest is not None and not latest.is_active
            else await jobs.latest_finished(db, user_guid, scope)
        )
        return {
            "building": latest is not None and latest.is_active,
            "last_result": _job_result(finished),
            "job": latest.public() if latest is not None else None,
        }

    async def scope_info(scope: str) -> dict[str, Any]:
        return {
            "nodes": await store.count_nodes(db, scope, user_guid),
            "edges": await store.count_edges(db, scope, user_guid),
            **await build_state(scope),
            "builds": await store.get_build_state(db, scope, user_guid),
            "plan": await asyncio.to_thread(plan_builder.plan_scope, scope, user_guid),
        }

    scopes = {"global": await scope_info("global")}

    rows = await db.fetch_all(
        """SELECT id, name FROM conversations
            WHERE kg_local_enabled = 1 AND is_active = 1 AND user_guid = ?""",
        (user_guid,),
    )
    conversation_graphs = []
    for row in rows:
        info = await scope_info(f"conv:{row['id']}")
        info.update({"conversation_id": row["id"], "name": row["name"]})
        conversation_graphs.append(info)

    return JSONResponse({"scopes": scopes, "conversation_graphs": conversation_graphs})


def _job_result(job: jobs.BuildJob | None) -> dict[str, Any] | None:
    """A finished job's outcome in the shape of an in-process build's result."""
    if job is None:
        return None
    result = dict(job.result or {})
    result["status"] = job.status
    if job.error is not None:
        result["error"] = job.error
    return result


@router.post("/api/build")
async def kg_build(request: Request) -> JSONResponse:
    """Start a build for a scope: queued for the worker on a server. 409 when already building."""
    data = await request.json()
    scope = str(data.get("scope", "")).strip()
    if refused := await _scope_refusal(request, scope):
        return refused
    if refused := _permission_refusal(request, scope):
        return refused

    user_guid = current_user_guid(request)
    model_id = await _build_model(request, user_guid)
    if not model_id:
        return JSONResponse(
            {"error": "No model to build with: set a default model or knowledge_graph.build_model"},
            status_code=409,
        )
    if is_server():
        job = await jobs.enqueue(database(request), user_guid, scope, model_id)
        if job is None:
            return _already_building()
        return JSONResponse({"status": "queued", "scope": scope, "job_id": job.id})
    return _build_in_process(request, user_guid, scope, model_id)


def _already_building() -> JSONResponse:
    return JSONResponse({"error": "A build is already running for this scope"}, status_code=409)


def _build_in_process(request: Request, user_guid: str, scope: str, model_id: str) -> JSONResponse:
    """Personal mode: build on a thread of this process, as before."""
    from spark.knowledge import builder
    from spark.knowledge.builder import KnowledgeGraphBuilder
    from spark.knowledge.tools import _get_embedder

    key = build_key(user_guid, scope)
    if not builder.try_acquire(key):
        return _already_building()

    conv_mgr = request.app.state.conversation_manager
    db = sync_database(request)
    tokens, results = _state_dicts(request)
    token = CancellationToken()
    tokens[key] = token

    def run() -> None:
        try:
            engine = KnowledgeGraphBuilder(db, conv_mgr._get_llm_service_for_model, _get_embedder())
            results[key] = engine.build_scope(scope, user_guid, model_id, cancel_token=token)
        except Exception as e:  # noqa: BLE001 - record, never crash the thread
            logger.error("Knowledge graph build failed: %s", e, exc_info=True)
            results[key] = {"status": "failed", "error": str(e)}
        finally:
            builder.release(key)
            tokens.pop(key, None)

    threading.Thread(target=run, daemon=True, name=f"kg-build-{scope}").start()
    return JSONResponse({"status": "started", "scope": scope})


async def _build_model(request: Request, user_guid: str) -> str:
    """The model a build extracts with, resolved explicitly (never the shared manager's).

    ``knowledge_graph.build_model`` first, then the user's default model (their
    preference on a server, ``config.yaml``'s in personal mode), then the first
    model listed; "" when there is none. The build gets a service of its own for it.
    """
    from spark.web.models import default_model

    ctx = getattr(request.app.state, "ctx", None)
    configured = ctx.settings.get("knowledge_graph.build_model") if ctx is not None else None
    if isinstance(configured, str) and configured:
        return configured
    preferred = await default_model(request, user_guid)
    if preferred:
        return preferred
    llm_manager = getattr(request.app.state, "llm_manager", None)
    listed = llm_manager.list_all_models() if llm_manager is not None else []
    return str(listed[0].get("id", "")) if listed else ""


@router.post("/api/cancel")
async def kg_cancel(request: Request) -> JSONResponse:
    data = await request.json()
    scope = str(data.get("scope", "")).strip()
    if refused := await _scope_refusal(request, scope):
        return refused
    if is_server():
        outcome = await jobs.request_cancel(database(request), current_user_guid(request), scope)
        return JSONResponse({"status": outcome})
    tokens, _ = _state_dicts(request)
    token = tokens.get(build_key(current_user_guid(request), scope))
    if token:
        token.cancel("user")
        return JSONResponse({"status": "cancelling"})
    return JSONResponse({"status": "not_building"})


@router.post("/api/retry")
async def kg_retry(request: Request) -> JSONResponse:
    """Queue one of the user's failed build jobs again (server mode)."""
    if not is_server():
        return JSONResponse(
            {"error": "Retry applies to queued builds on a server; start a new build"},
            status_code=404,
        )
    data = await request.json()
    raw = data.get("job_id") if isinstance(data, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return JSONResponse({"error": "job_id must be a number"}, status_code=400)
    db = database(request)
    user_guid = current_user_guid(request)
    found = await jobs.get(db, user_guid, raw)
    if found is None:
        return not_found()
    if refused := _permission_refusal(request, found.scope):
        return refused
    outcome, job = await jobs.retry(db, user_guid, raw)
    if outcome == "not_found" or job is None:
        return not_found()
    if outcome == "not_failed":
        return JSONResponse({"error": "Only a failed build can be retried"}, status_code=409)
    if outcome == "busy":
        return _already_building()
    return JSONResponse({"status": "queued", "scope": job.scope, "job_id": job.id})


@router.get("/api/graph")
async def kg_graph(request: Request, scope: str) -> JSONResponse:
    """Nodes and edges for the visualisation, capped, without embeddings."""
    from spark.knowledge import store

    if refused := await _scope_refusal(request, scope):
        return refused

    db = database(request)
    user_guid = current_user_guid(request)
    total_nodes = await store.count_nodes(db, scope, user_guid)
    total_edges = await store.count_edges(db, scope, user_guid)
    nodes = await store.get_nodes(db, scope, user_guid, limit=_GRAPH_NODE_CAP)
    included = {n["id"] for n in nodes}
    edges = [
        e
        for e in await store.get_edges(db, scope, user_guid)
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

    if refused := await _scope_refusal(request, scope):
        return refused

    results = await asyncio.to_thread(
        find_entities,
        sync_database(request),
        _get_embedder(),
        [scope],
        q,
        current_user_guid(request),
    )
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
    if refused := await _scope_refusal(request, scope):
        return refused
    if refused := _permission_refusal(request, scope):
        return refused
    user_guid = current_user_guid(request)
    if is_server():
        building = await jobs.active(database(request), user_guid, scope) is not None
    else:
        building = builder.is_building(build_key(user_guid, scope))
    if building:
        return JSONResponse({"error": "Cannot clear while building"}, status_code=409)
    await store.clear_scope(database(request), scope, user_guid)
    return JSONResponse({"status": "ok"})
