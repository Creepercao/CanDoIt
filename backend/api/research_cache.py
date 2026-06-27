"""REST API for the research knowledge base — inspect, search, and manage
cached research results.
"""

import logging
from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.research_cache import get_knowledge_base, get_research_cache

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
logger = logging.getLogger("api.knowledge")


# ── Search ─────────────────────────────────────────────────────────────

@router.get("/search")
async def search_knowledge(
    q: str = Query(..., description="Search query"),
    top_k: int = Query(5, ge=1, le=20),
    threshold: float = Query(0.7, ge=0.0, le=1.0),
):
    """Search the knowledge base for similar past research results."""
    kb = get_knowledge_base()
    if not kb:
        return {"results": [], "error": "Knowledge base not available"}
    results = await kb.search(q, top_k=top_k, threshold=threshold)
    return {"results": results, "query": q, "count": len(results)}


# ── Stats ──────────────────────────────────────────────────────────────

@router.get("/stats")
async def knowledge_stats():
    """Get knowledge base statistics."""
    kb = get_knowledge_base()
    if not kb:
        return {"status": "unavailable"}
    return await kb.stats()


# ── Entries ────────────────────────────────────────────────────────────

@router.get("/entries")
async def list_entries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List entries in the knowledge base (paginated)."""
    kb = get_knowledge_base()
    if not kb:
        return {"entries": [], "total": 0, "page": page, "error": "Knowledge base not available"}
    return await kb.list_entries(page=page, page_size=page_size)


class DeleteRequest(BaseModel):
    entry_id: str


@router.delete("/entries/{entry_id}")
async def delete_entry(entry_id: str):
    """Delete a single entry by ID."""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "Knowledge base not available"}
    ok = await kb.delete_entry(entry_id)
    return {"deleted": ok, "entry_id": entry_id}


# ── Clear ──────────────────────────────────────────────────────────────

@router.delete("")
async def clear_knowledge():
    """Clear all entries from the knowledge base and semantic cache."""
    kb = get_knowledge_base()
    rc = get_research_cache()

    kb_count = 0
    if kb:
        kb_count = await kb.clear()

    if rc:
        await rc.invalidate()

    return {
        "deleted": kb_count,
        "message": f"Cleared {kb_count} knowledge base entries + semantic cache",
    }


# ── Reindex ────────────────────────────────────────────────────────────

@router.post("/reindex")
async def reindex_knowledge():
    """Rebuild / verify the knowledge base index."""
    kb = get_knowledge_base()
    if not kb:
        return {"error": "Knowledge base not available"}

    stats = await kb.stats()
    return {
        "status": "ok",
        "message": f"Knowledge base ready with {stats.get('total_entries', 0)} entries",
        **stats,
    }
