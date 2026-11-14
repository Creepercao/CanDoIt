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


@pytest.mark.parametrize("query,top_k,threshold", [("", 5, .4), (None, 5, .4), ("q", 0, .4),
    ("q", True, .4), ("q", 201, .4), ("q", 1.5, .4), ("q", 5, float("nan")),
    ("q", 5, True), ("q", 5, "0.4"), ("q", 5, 2)])
def test_search_rejects_bad_configuration_before_initialization(kb, monkeypatch, query, top_k, threshold):
    async def forbidden():
        pytest.fail("invalid search must not initialize the database")

    monkeypatch.setattr(kb, "_ensure_init", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(kb.search(query, top_k, threshold))


@pytest.mark.parametrize("bad", [None, True, "broken", float("nan"), float("inf"), -float("inf")])
def test_invalid_distance_is_never_returned_as_a_hit(kb, monkeypatch, bad):
    monkeypatch.setattr(kb._collection, "query", lambda **kwargs: {
        "ids": [["invalid", "valid", "missing"]], "distances": [[bad, .1]],
        "documents": [["bad", "good", "missing"]], "metadatas": [[{}, {}, {}]],
    })
    results = asyncio.run(kb.search("q", threshold=-1))
    assert [item["id"] for item in results] == ["valid"]
    assert results[0]["similarity"] == .9


@pytest.mark.parametrize("failure", ["init", "unavailable", "embedding", "embedding_error", "query", "distance"])
def test_strict_search_distinguishes_failure_from_empty_results(kb, monkeypatch, failure):
    async def failed_init():
        raise OSError("database failed")

    async def unavailable_embedding(*args):
        return None

    async def failed_embedding(*args):
        raise OSError("provider failed")

    def failed_query(**kwargs):
        raise OSError("query failed")

    if failure == "init":
        monkeypatch.setattr(kb, "_ensure_init", failed_init)
    elif failure == "unavailable":
        kb._collection = None
    elif failure == "embedding":
        monkeypatch.setattr(rc, "get_embedding", unavailable_embedding)
    elif failure == "embedding_error":
        monkeypatch.setattr(rc, "get_embedding", failed_embedding)
    elif failure == "query":
        monkeypatch.setattr(kb._collection, "query", failed_query)
    else:
        monkeypatch.setattr(kb._collection, "query", lambda **kwargs: {"ids": [["bad"]], "distances": [[None]]})
    with pytest.raises(rc.KnowledgeSearchError):
        asyncio.run(kb.search("q", strict=True))
    assert asyncio.run(kb.search("q")) == []


def test_strict_search_allows_successful_empty_retrieval(kb):
    assert asyncio.run(kb.search("q", threshold=.99, strict=True)) == []
