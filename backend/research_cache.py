"""Research result caching and knowledge base — 方案 A + B.

方案 A — Semantic query cache (Redis / in-memory via ``Cache``):
    Exact-hash lookup first, then embedding-based similarity matching
    for semantically similar queries.  Degrades gracefully when no
    embedding endpoint is available.

方案 B — ChromaDB knowledge base:
    Embedded vector store persisted to ``outputs/chroma_db/``.
    Stores research syntheses with metadata for retrieval across
    sessions.  No external service required.

Integration points are in ``research_worker`` (builtin_workers.py) and
``search_and_scrape`` (data_scraper.py).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from backend.cache import cache
from backend.embedding import (
    cosine_similarity,
    get_embedding,
    embedding_available,
)

logger = logging.getLogger("research_cache")

# ── Configuration ──────────────────────────────────────────────────────

RESEARCH_CACHE_TTL = int(os.environ.get("RESEARCH_CACHE_TTL", "86400"))  # 24 h
RESEARCH_CACHE_THRESHOLD = float(
    os.environ.get("RESEARCH_CACHE_THRESHOLD", "0.85")
)
RESEARCH_CACHE_MAX_ENTRIES = int(
    os.environ.get("RESEARCH_CACHE_MAX_ENTRIES", "500")
)

# ChromaDB persistence directory (under project root outputs/)
CHROMA_PERSIST_DIR = (
    Path(__file__).parent.parent / "outputs" / "chroma_db"
)


# ── Pure-Python LRU helper ─────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── 方案 A: Semantic Query Cache ───────────────────────────────────────

class ResearchCache:
    """Semantic cache for research results.

    Layer 1: exact query-hash match (fast, Redis/memory backed)
    Layer 2: embedding similarity search (requires LLM provider /embeddings endpoint)

    Falls back gracefully when embeddings are unavailable.
    """

    def __init__(
        self,
        similarity_threshold: float = RESEARCH_CACHE_THRESHOLD,
        ttl: int = RESEARCH_CACHE_TTL,
        max_entries: int = RESEARCH_CACHE_MAX_ENTRIES,
    ):
        self._threshold = similarity_threshold
        self._ttl = ttl
        self._max_entries = max_entries
        self._embeddings_key = "research:embeddings"

    # ── Public API ──────────────────────────────────────────────────

    async def get_similar(self, query: str) -> Optional[dict]:
        """Return a cached research result if a similar query was seen before.

        Returns the full result dict on hit, ``None`` on miss.
        """
        if not query:
            return None

        # Layer 1: exact hash match
        exact_key = f"research:{cache.hash_key(query)}"
        cached = await cache.get(exact_key)
        if cached:
            logger.info(f"Research cache HIT (exact): {query[:60]}...")
            return cached

        # Layer 2: embedding similarity
        query_emb = await get_embedding(query[:500])
        if query_emb is None:
            return None

        stored = await cache.get(self._embeddings_key)
        if not stored or not isinstance(stored, list):
            return None

        best_score = 0.0
        best_key = ""

        for entry in stored:
            if not isinstance(entry, dict):
                continue
            emb = entry.get("embedding")
            if not emb or len(emb) != len(query_emb):
                continue
            sim = cosine_similarity(query_emb, emb)
            if sim > best_score:
                best_score = sim
                best_key = entry.get("key", "")

        if best_score >= self._threshold and best_key:
            cached = await cache.get(best_key)
            if cached:
                logger.info(
                    f"Research cache HIT (semantic, sim={best_score:.3f}): "
                    f"{query[:60]}..."
                )
                return cached

        return None

    async def store(self, query: str, result: dict) -> None:
        """Store a research result for future reuse."""
        if not query:
            return

        exact_key = f"research:{cache.hash_key(query)}"

        # Wrap result with cache metadata
        entry = {
            **result,
            "_cached_at": _now_iso(),
            "_original_query": query[:500],
        }
        await cache.set(exact_key, entry, ttl=self._ttl)

        # Store embedding for similarity matching
        query_emb = await get_embedding(query[:500])
        if query_emb is not None:
            await self._store_embedding(exact_key, query[:500], query_emb)

        logger.info(f"Research cache stored: {query[:60]}...")

    async def invalidate(self, query: str = "") -> None:
        """Invalidate cache entries.  If *query* is empty, clear all."""
        if query:
            exact_key = f"research:{cache.hash_key(query)}"
            await cache.delete(exact_key)
        else:
            # Clear all research cache entries (best effort)
            stored = await cache.get(self._embeddings_key)
            if stored and isinstance(stored, list):
                for entry in stored:
                    if isinstance(entry, dict) and entry.get("key"):
                        await cache.delete(entry["key"])
            await cache.delete(self._embeddings_key)
            logger.info("Research cache cleared")

    # ── Internal ────────────────────────────────────────────────────

    async def _store_embedding(
        self, key: str, query: str, embedding: list[float]
    ) -> None:
        """Add query embedding to the stored embeddings list (LRU capped)."""
        stored = await cache.get(self._embeddings_key)
        if not stored or not isinstance(stored, list):
            stored = []

        # Remove existing entry for same key (update)
        stored = [e for e in stored if e.get("key") != key]

        stored.append({
            "key": key,
            "query": query[:200],
            "embedding": embedding,
            "stored_at": _now_iso(),
        })

        # LRU eviction — keep only the most recent N entries
        if len(stored) > self._max_entries:
            removed = stored[: len(stored) - self._max_entries]
            for old in removed:
                await cache.delete(old.get("key", ""))
            stored = stored[-self._max_entries:]

        # Redis can expire individual keys, but we keep the index with a
        # longer TTL so entries can be found even if some keys expire.
        await cache.set(self._embeddings_key, stored, ttl=self._ttl * 7)


# ── 方案 B: ChromaDB Knowledge Base ────────────────────────────────────

class KnowledgeBase:
    """Persistent knowledge base backed by embedded ChromaDB.

    Stores research syntheses as documents with vector embeddings.
    Automatically persists to disk — survives restarts.
    """

    def __init__(self, persist_dir: Optional[Path] = None):
        self._dir = persist_dir or CHROMA_PERSIST_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        self._collection = None
        self._initialized = False

    # ── Lazy init ───────────────────────────────────────────────────

    async def _ensure_init(self):
        if self._initialized:
            return
        try:
            import chromadb

            self._client = chromadb.PersistentClient(
                path=str(self._dir),
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._collection = self._client.get_or_create_collection(
                name="research_knowledge",
                metadata={"description": "Research results knowledge base"},
            )
            self._initialized = True
            logger.info(
                f"KnowledgeBase ready: {self._collection.count()} entries "
                f"at {self._dir}"
            )
        except ImportError:
            logger.warning("chromadb not installed — KnowledgeBase disabled")
        except Exception as e:
            logger.warning(f"KnowledgeBase init failed: {e}")

    @property
    def available(self) -> bool:
        return self._initialized and self._collection is not None

    # ── Public API ──────────────────────────────────────────────────

    async def add(self, query: str, result: dict) -> Optional[str]:
        """Add a research result to the knowledge base.

        Returns the entry ID on success, ``None`` on failure.
        """
        await self._ensure_init()
        if not self.available:
            return None

        synthesis = result.get("synthesis", "") or result.get("raw_text", "")
        if not synthesis or len(synthesis) < 50:
            return None

        entry_id = cache.hash_key(f"{query}:{_now_iso()}")

        metadata = {
            "query": query[:300],
            "task": result.get("task", query)[:300],
            "stored_at": _now_iso(),
            "source_count": len(result.get("sources", [])),
            "has_structured_data": bool(result.get("structured_data")),
        }

        # Generate embedding
        embedding = await get_embedding(synthesis[:2000])
        if embedding is None:
            logger.debug("No embedding available — storing document-only")
            try:
                self._collection.add(
                    documents=[synthesis[:4000]],
                    metadatas=[metadata],
                    ids=[entry_id],
                )
                return entry_id
            except Exception as e:
                logger.warning(f"KnowledgeBase add (no emb) failed: {e}")
                return None

        try:
            self._collection.add(
                documents=[synthesis[:4000]],
                embeddings=[embedding],
                metadatas=[metadata],
                ids=[entry_id],
            )
            logger.info(f"KnowledgeBase added: {entry_id}")
            return entry_id
        except Exception as e:
            logger.warning(f"KnowledgeBase add failed: {e}")
            return None

    async def search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.7,
    ) -> list[dict]:
        """Search the knowledge base for similar past research results."""
        await self._ensure_init()
        if not self.available:
            return []

        embedding = await get_embedding(query[:500])
        n_results = min(top_k, 20)

        try:
            if embedding is not None:
                results = self._collection.query(
                    query_embeddings=[embedding],
                    n_results=n_results,
                    include=["documents", "metadatas", "distances"],
                )
            else:
                # Fallback: use ChromaDB's built-in (if any) or skip
                results = self._collection.query(
                    query_texts=[query[:500]],
                    n_results=n_results,
                    include=["documents", "metadatas", "distances"],
                )
        except Exception as e:
            logger.warning(f"KnowledgeBase search error: {e}")
            return []

        entries: list[dict] = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for i, entry_id in enumerate(ids):
            distance = distances[i] if i < len(distances) else 1.0
            # ChromaDB returns cosine distance for cosine collections
            similarity = 1.0 - float(distance) if distance is not None else 0.0
            if similarity < threshold:
                continue
            entries.append({
                "id": entry_id,
                "content": docs[i][:800] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "similarity": round(similarity, 4),
            })

        return entries

    async def list_entries(
        self, page: int = 1, page_size: int = 20
    ) -> dict:
        """List entries in the knowledge base (paginated)."""
        await self._ensure_init()
        if not self.available:
            return {"entries": [], "total": 0, "page": page}

        try:
            total = self._collection.count()
            offset = (page - 1) * page_size
            # ChromaDB get with offset/limit
            result = self._collection.get(
                limit=min(page_size, 100),
                offset=offset,
                include=["metadatas", "documents"],
            )
            entries = []
            ids = result.get("ids", [])
            metas = result.get("metadatas", [])
            docs = result.get("documents", [])
            for i, eid in enumerate(ids):
                entries.append({
                    "id": eid,
                    "metadata": metas[i] if i < len(metas) else {},
                    "content_preview": (
                        docs[i][:300] + "..." if i < len(docs) and docs[i] else ""
                    ),
                })
            return {"entries": entries, "total": total, "page": page}
        except Exception as e:
            logger.warning(f"KnowledgeBase list_entries error: {e}")
            return {"entries": [], "total": 0, "page": page, "error": str(e)}

    async def delete_entry(self, entry_id: str) -> bool:
        """Delete a single entry by ID."""
        await self._ensure_init()
        if not self.available:
            return False
        try:
            self._collection.delete(ids=[entry_id])
            logger.info(f"KnowledgeBase deleted: {entry_id}")
            return True
        except Exception as e:
            logger.warning(f"KnowledgeBase delete error: {e}")
            return False

    async def clear(self) -> int:
        """Delete all entries.  Returns the count of removed entries."""
        await self._ensure_init()
        if not self.available:
            return 0
        try:
            count = self._collection.count()
            # Delete all by getting all IDs first
            ids = self._collection.get(limit=10000, include=[])["ids"]
            if ids:
                self._collection.delete(ids=ids)
            logger.info(f"KnowledgeBase cleared: {count} entries")
            return count
        except Exception as e:
            logger.warning(f"KnowledgeBase clear error: {e}")
            return 0

    async def stats(self) -> dict:
        """Return knowledge base statistics."""
        await self._ensure_init()
        if not self.available:
            return {"status": "unavailable"}
        try:
            count = self._collection.count()
            # Estimate disk usage
            disk_size = 0
            if self._dir.exists():
                for f in self._dir.rglob("*"):
                    if f.is_file():
                        disk_size += f.stat().st_size
            return {
                "status": "available",
                "total_entries": count,
                "disk_size_bytes": disk_size,
                "disk_size_mb": round(disk_size / (1024 * 1024), 2),
                "persist_dir": str(self._dir),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


# ── Module singletons ──────────────────────────────────────────────────

_research_cache: Optional[ResearchCache] = None
_knowledge_base: Optional[KnowledgeBase] = None


def get_research_cache() -> ResearchCache:
    """Get or create the ResearchCache singleton (方案 A)."""
    global _research_cache
    if _research_cache is None:
        _research_cache = ResearchCache()
    return _research_cache


def get_knowledge_base() -> KnowledgeBase:
    """Get or create the KnowledgeBase singleton (方案 B)."""
    global _knowledge_base
    if _knowledge_base is None:
        _knowledge_base = KnowledgeBase()
    return _knowledge_base


async def check_cache_or_search(
    query: str,
    search_fn,
) -> dict:
    """Check cache first; on miss, run *search_fn* and cache the result.

    *search_fn* must be an async callable that returns a dict with at least
    the keys ``synthesis``, ``sources``, and ``structured_data``.
    """
    rc = get_research_cache()

    # 方案 A: check semantic cache
    cached = await rc.get_similar(query)
    if cached:
        return cached

    # Cache miss — run the actual search
    result = await search_fn()

    # Store in both cache layers
    await rc.store(query, result)

    kb = get_knowledge_base()
    if kb:
        try:
            await kb.add(query, result)
        except Exception as e:
            logger.debug(f"KnowledgeBase store skipped: {e}")

    return result
