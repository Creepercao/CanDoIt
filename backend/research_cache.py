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
#
# The thresholds below come from an actual calibration run against the provider
# in use (SiliconFlow / BAAI/bge-large-zh-v1.5, 1024-dim), re-runnable with:
#
#     python -m backend.tools.threshold_calibration
#
# Measured cosine similarity over 12 pairs per group:
#
#     paraphrase (should hit)      min 0.433  p25 0.743  median 0.796  max 0.918
#     same_topic (must NOT hit)    min 0.574  p25 0.659  median 0.770  max 0.904
#     unrelated  (must NOT hit)    min 0.050  p25 0.156  median 0.207  max 0.281
#
# The paraphrase and same_topic distributions **overlap almost completely**, so
# no single cosine threshold can separate "same question reworded" from "same
# topic, different question" — the worst negative scored 0.904
# ("读取大文件" vs "写入大文件"), higher than 75% of the true positives.
# Adding BGE's query instruction prefix was measured too and did not help
# (separation -0.474 vs -0.472).
#
# Therefore a high similarity is treated as *evidence of relatedness*, never as
# proof of equivalence:
#
#     >= HIT   (0.92)  near-duplicate → serve directly
#     >= FLOOR (0.40)  related but ambiguous → ask a model whether the two
#                      queries share one information need; only then serve
#     <  FLOOR         miss
#
# If the confirmation model is unavailable the candidate is rejected, so the
# cache can never silently answer a different question.

RESEARCH_CACHE_TTL = int(os.environ.get("RESEARCH_CACHE_TTL", "86400"))  # 24 h

# Near-duplicate: served without confirmation.  Above every measured negative.
RESEARCH_CACHE_THRESHOLD = float(
    os.environ.get("RESEARCH_CACHE_THRESHOLD", "0.92")
)
# Related-but-ambiguous floor: the boundary where measured `unrelated` pairs stop
# (max 0.281) with margin.  Candidates in [FLOOR, HIT) need confirmation.
RESEARCH_CACHE_SIMILARITY_FLOOR = float(
    os.environ.get("RESEARCH_CACHE_SIMILARITY_FLOOR", "0.40")
)
RESEARCH_CACHE_MAX_ENTRIES = int(
    os.environ.get("RESEARCH_CACHE_MAX_ENTRIES", "500")
)
# Set to 0 to disable the confirmation step (then only near-duplicates hit).
RESEARCH_CACHE_CONFIRM = os.environ.get(
    "RESEARCH_CACHE_CONFIRM", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Knowledge-base / memory retrieval only *surfaces* candidates (a human sees the
# list, or a model can ignore an item), so the cost of a marginal hit is low and
# the floor sits just above the measured `unrelated` ceiling.
RETRIEVAL_SIMILARITY_DEFAULT = float(
    os.environ.get("RETRIEVAL_SIMILARITY_THRESHOLD", "0.40")
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
        similarity_floor: float = RESEARCH_CACHE_SIMILARITY_FLOOR,
        confirm: bool = RESEARCH_CACHE_CONFIRM,
        model_id: str = "",
    ):
        self._threshold = similarity_threshold
        self._floor = min(similarity_floor, similarity_threshold)
        self._ttl = ttl
        self._max_entries = max_entries
        self._confirm = confirm
        self._model_id = model_id
        self._embeddings_key = "research:embeddings"

    # ── Public API ──────────────────────────────────────────────────

    async def get_similar(self, query: str) -> Optional[dict]:
        """Return a cached research result if a similar query was seen before.

        Layer 2 is **two-stage** because the calibration shows cosine similarity
        cannot tell "same question reworded" from "same topic, different
        question": a near-duplicate is served directly, anything merely related
        must be confirmed by a model first.  Returns the result dict on hit.
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
        best_query = ""

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
                best_query = entry.get("query", "") or entry.get("text", "")

        if not best_key:
            logger.info(f"Research cache MISS (no candidates): {query[:60]}...")
            return None

        # Stage 1 — near-duplicate: safe to serve as-is.
        if best_score >= self._threshold:
            cached = await cache.get(best_key)
            if cached:
                logger.info(
                    f"Research cache HIT (near-duplicate sim={best_score:.3f}): "
                    f"{query[:60]}..."
                )
                return cached
            return None

        # Stage 2 — related but ambiguous: confirm the information need matches.
        if best_score >= self._floor and self._confirm:
            if await self._same_information_need(query, best_query):
                cached = await cache.get(best_key)
                if cached:
                    logger.info(
                        f"Research cache HIT (confirmed sim={best_score:.3f}): "
                        f"{query[:60]}..."
                    )
                    return cached
            else:
                logger.info(
                    f"Research cache REJECTED (sim={best_score:.3f} but a "
                    f"different question): {query[:60]}... vs {best_query[:60]}..."
                )
                return None

        # Miss — but report the best score so the threshold stays observable.
        logger.info(
            f"Research cache MISS (best sim={best_score:.3f} < "
            f"floor {self._floor:.2f}): {query[:60]}..."
        )
        return None

    async def _same_information_need(self, query: str, candidate: str) -> bool:
        """Ask a model whether two queries want the same facts.

        Fails **closed**: any error means "not the same", so an unavailable
        judge can never turn into a silently wrong cache hit.
        """
        if not candidate or candidate.strip() == query.strip():
            return bool(candidate)
        from backend.prompts import CACHE_EQUIVALENCE_PROMPT

        try:
            import asyncio
            import json
            import re

            from langchain_core.messages import HumanMessage

            from backend.models.provider import create_chat_model

            llm = create_chat_model(
                model_id=self._model_id or "deepseek-ai/DeepSeek-V3",
                temperature=0.0, max_tokens=200,
            )
            response = await asyncio.wait_for(
                llm.ainvoke([HumanMessage(content=CACHE_EQUIVALENCE_PROMPT.format(
                    a=query[:400], b=candidate[:400]))]),
                timeout=20.0,
            )
        except Exception as exc:
            logger.debug("Cache equivalence check unavailable: %s", exc)
            return False

        content = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return False
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return False
        return bool(isinstance(data, dict) and data.get("same"))

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
                embedding_function=None,  # we provide embeddings manually
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

        # Stable id per query: re-researching the same question must overwrite
        # its entry instead of piling up near-duplicates in the knowledge base.
        entry_id = cache.hash_key(query)

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
                self._collection.upsert(
                    documents=[synthesis[:4000]],
                    metadatas=[metadata],
                    ids=[entry_id],
                )
                return entry_id
            except Exception as e:
                logger.warning(f"KnowledgeBase add (no emb) failed: {e}")
                return None

        try:
            self._collection.upsert(
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
        threshold: float = RETRIEVAL_SIMILARITY_DEFAULT,
    ) -> list[dict]:
        """Search the knowledge base for similar past research results."""
        await self._ensure_init()
        if not self.available:
            return []

        embedding = await get_embedding(query[:500])
        # Over-fetch, then filter by threshold, then trim. Fetching only top_k
        # candidates first would silently drop good hits that rank just below
        # the cut-off, leaving an empty result even when the base has a match.
        n_results = min(max(top_k * 10, 50), 200)

        try:
            if embedding is not None:
                results = self._collection.query(
                    query_embeddings=[embedding],
                    n_results=n_results,
                    include=["documents", "metadatas", "distances"],
                )
            else:
                # No embedding available — skip (don't trigger ONNX download)
                return []
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

        return entries[:top_k]

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
