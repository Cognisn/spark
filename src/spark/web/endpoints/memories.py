"""Memory management endpoints."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories")


def _get_memory_index(request: Request) -> Any:
    """Get or create a MemoryIndex from app state."""
    idx = getattr(request.app.state, "_memory_index", None)
    if idx is None:
        db = getattr(request.app.state, "database", None)
        if not db:
            return None
        from spark.index.memory_index import MemoryIndex

        user_guid = getattr(request.app.state, "user_guid", "default")
        idx = MemoryIndex(db.connection, user_guid)
        request.app.state._memory_index = idx
    return idx


@router.get("", response_class=HTMLResponse)
async def memories_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "memories.html")


@router.get("/api/list")
async def list_memories(request: Request) -> JSONResponse:
    category = request.query_params.get("category")
    limit = int(request.query_params.get("limit", "100"))
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"memories": [], "total": 0})
    memories = idx.list_all(category=category, limit=limit)
    return JSONResponse({"memories": memories, "total": len(memories)})


@router.get("/api/stats")
async def memory_stats(request: Request) -> JSONResponse:
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"total": 0, "by_category": {}, "avg_importance": 0})
    all_mems = idx.list_all(limit=10000)
    total = len(all_mems)
    by_cat: dict[str, int] = {}
    imp_sum = 0.0
    for m in all_mems:
        cat = m.get("category", "facts")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        imp_sum += m.get("importance", 0.5)
    return JSONResponse(
        {
            "total": total,
            "by_category": by_cat,
            "avg_importance": round(imp_sum / total, 2) if total else 0,
        }
    )


@router.post("/api/create")
async def create_memory(request: Request) -> JSONResponse:
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)
    data = await request.json()
    content = data.get("content", "").strip()
    category = data.get("category", "facts")
    importance = float(data.get("importance", 0.5))
    if not content:
        return JSONResponse({"error": "Content required"}, status_code=400)
    mid = idx.store(content, category, importance=importance)
    if mid is None:
        return JSONResponse({"error": "Duplicate memory"}, status_code=409)
    return JSONResponse({"status": "ok", "id": mid})


@router.put("/api/{memory_id}")
async def update_memory(request: Request, memory_id: int) -> JSONResponse:
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)
    data = await request.json()
    success = idx.update(
        memory_id,
        content=data.get("content"),
        category=data.get("category"),
        importance=data.get("importance"),
    )
    if not success:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"status": "ok"})


@router.delete("/api/{memory_id}")
async def delete_memory(request: Request, memory_id: int) -> JSONResponse:
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)
    success = idx.delete(memory_id)
    if not success:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"status": "ok"})


@router.post("/api/delete-all")
async def delete_all_memories(request: Request) -> JSONResponse:
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if data.get("confirm") != "DELETE_ALL":
        return JSONResponse({"error": "Confirmation required"}, status_code=400)
    idx.clear_all()
    return JSONResponse({"status": "ok"})


@router.get("/api/export")
async def export_memories(request: Request):  # type: ignore[no-untyped-def]
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)
    all_mems = idx.list_all(limit=100000)
    export = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_count": len(all_mems),
        "memories": all_mems,
    }
    content = json.dumps(export, indent=2, default=str)
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="memories_export_{now}.json"'},
    )


@router.post("/api/import")
async def import_memories(request: Request) -> JSONResponse:
    idx = _get_memory_index(request)
    if not idx:
        return JSONResponse({"error": "Not initialised"}, status_code=503)
    data = await request.json()
    memories = data.get("memories", [])
    if not memories:
        return JSONResponse({"error": "No memories to import"}, status_code=400)
    imported = 0
    skipped = 0
    for m in memories:
        content = m.get("content", "")
        category = m.get("category", "facts")
        importance = float(m.get("importance", 0.5))
        if not content:
            skipped += 1
            continue
        mid = idx.store(content, category, importance=importance)
        if mid is None:
            skipped += 1
        else:
            imported += 1
    return JSONResponse({"status": "ok", "imported": imported, "skipped": skipped})
