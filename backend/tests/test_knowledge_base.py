"""Knowledge-base retrieval behaviour.

Two defects motivated these tests:

1. ``search`` used to ask ChromaDB for only ``top_k`` candidates *before*
   applying the similarity threshold, so a query could come back empty even
   though a good match existed — it simply ranked below the cut.
2. Entries were keyed by ``query + timestamp``, so asking the same question
   twice piled up near-duplicate rows instead of overwriting.
"""
from __future__ import annotations

import asyncio

import pytest

from backend import research_cache as rc


class _FakeCollection:
    """Minimal stand-in so the tests never touch a real ChromaDB."""

    def __init__(self, total: int = 60):
        self.rows: dict[str, str] = {}
        self.total = total
        self.last_n_results: int | None = None

    # write path
    def upsert(self, documents=None, metadatas=None, ids=None, embeddings=None):
        for index, entry_id in enumerate(ids or []):
            self.rows[entry_id] = documents[index]

    def add(self, **kwargs):  # pragma: no cover - guard against regressions
        raise AssertionError("knowledge base must upsert, not add")

    def count(self) -> int:
        return len(self.rows)

    # read path
    def query(self, query_embeddings=None, n_results=None, include=None):
        self.last_n_results = n_results
        n = min(n_results or 0, self.total)
        return {
            "ids": [[f"e{i}" for i in range(n)]],
            "documents": [["doc" * 20] * n],
            "metadatas": [[{}] * n],
            "distances": [[0.1] * n],  # similarity 0.9 — well inside the threshold
        }


@pytest.fixture
def kb(monkeypatch):
    """A KnowledgeBase bypassing __init__ and ChromaDB."""
    instance = rc.KnowledgeBase.__new__(rc.KnowledgeBase)
    instance._initialized = True
    instance._collection = _FakeCollection()

    async def _fake_embedding(text: str):
        return [0.1] * 8

    monkeypatch.setattr(rc, "get_embedding", _fake_embedding)
    return instance


def _result(synthesis: str) -> dict:
    return {"synthesis": synthesis, "sources": [{"url": "https://example.com"}]}


def test_repeated_query_overwrites_instead_of_duplicating(kb):
    async def scenario():
        await kb.add("什么是 HTTP", _result("第一次的研究结论，" * 10))
        await kb.add("什么是 HTTP", _result("第二次更新的研究结论，" * 10))

    asyncio.run(scenario())

    assert len(kb._collection.rows) == 1, "the same question must not create two rows"


def test_different_queries_get_separate_entries(kb):
    async def scenario():
        await kb.add("什么是 HTTP", _result("传输协议，" * 20))
        await kb.add("什么是 TCP", _result("传输层协议，" * 20))

    asyncio.run(scenario())

    assert len(kb._collection.rows) == 2


def test_search_overfetches_before_applying_the_threshold(kb):
    """A match ranked below top_k must still be found."""

    async def scenario():
        return await kb.search("随便问问", top_k=5, threshold=0.5)

    entries = asyncio.run(scenario())

    assert kb._collection.last_n_results is not None
    assert kb._collection.last_n_results >= 50, "must fetch more than top_k candidates"
    assert len(entries) == 5, "results are trimmed to top_k after filtering"


def test_search_returns_nothing_when_below_threshold(kb):
    async def scenario():
        return await kb.search("随便问问", top_k=5, threshold=0.99)

    entries = asyncio.run(scenario())
    assert entries == []


def test_add_rejects_too_short_synthesis(kb):
    async def scenario():
        return await kb.add("短问题", {"synthesis": "太短"})

    assert asyncio.run(scenario()) is None
    assert len(kb._collection.rows) == 0
