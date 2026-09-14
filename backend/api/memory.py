"""Cross-session memory API.

``GET    /api/memory``            list memories (filter by status/kind)
``GET    /api/memory/stats``      counts by status and kind
``POST   /api/memory``            add one manually
``POST   /api/memory/search``     semantic recall probe
``POST   /api/memory/{id}/confirm``  promote a pending conclusion to active
``POST   /api/memory/{id}/reject``   mark a memory as wrong
``DELETE /api/memory/{id}``       delete a memory

Pending items are the ones the *model* proposed but a human has not approved;
they are never injected into a prompt until confirmed.  A memory added through
``POST /api/memory`` is human-authored, so it starts ``active`` unless the
caller explicitly asks for ``pending``.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.memory import (
    ACTIVE,
    KINDS,
    PENDING,
    get_memory_store,
    memory_enabled,
)

router = APIRouter(tags=["memory"])
logger = logging.getLogger("api.memory")


class MemoryCreate(BaseModel):
    text: str
    kind: str = "fact"
    tags: list[str] = []
    status: Optional[str] = None
    confidence: float = 1.0


class MemorySearch(BaseModel):
    query: str
    top_k: int = 5
    min_score: float = 0.0


@router.get("/memory/stats")
async def memory_stats():
    store = get_memory_store()
    return {"enabled": memory_enabled(), **store.stats()}


@router.get("/memory")
async def memory_list(status: str = "", kind: str = "", limit: int = 200):
    store = get_memory_store()
    items = store.items(status=status, kind=kind, limit=max(1, min(limit, 500)))
    return {
        "enabled": memory_enabled(),
        "memories": [item.to_dict() for item in items],
        "count": len(items),
        "stats": store.stats(),
    }


@router.post("/memory/search")
async def memory_search(payload: MemorySearch):
    store = get_memory_store()
    results = await store.recall(
        payload.query,
        top_k=max(1, min(payload.top_k, 20)),
        min_score=max(0.0, payload.min_score),
    )
    return {"query": payload.query, "results": results, "count": len(results)}


@router.post("/memory")
async def memory_create(payload: MemoryCreate):
    if payload.kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"kind 必须是 {list(KINDS)} 之一")
    store = get_memory_store()
    item = await store.add(
        payload.text,
        kind=payload.kind,
        tags=payload.tags,
        status=payload.status or ACTIVE,
        confidence=payload.confidence,
    )
    if item is None:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    return {"ok": True, "memory": item.to_dict()}


@router.post("/memory/{memory_id}/confirm")
async def memory_confirm(memory_id: str):
    item = get_memory_store().confirm(memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    logger.info("Memory confirmed: %s", memory_id)
    return {"ok": True, "memory": item.to_dict()}


@router.post("/memory/{memory_id}/reject")
async def memory_reject(memory_id: str):
    item = get_memory_store().reject(memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True, "memory": item.to_dict()}


@router.delete("/memory/{memory_id}")
async def memory_delete(memory_id: str):
    if not get_memory_store().delete(memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True, "memory_id": memory_id}


@router.get("/memory/pending/count")
async def memory_pending_count():
    """Cheap poll for the sidebar badge."""
    stats = get_memory_store().stats()
    return {"pending": stats["by_status"].get(PENDING, 0), "enabled": memory_enabled()}
