"""Tests for the two-stage research-cache gate.

Calibration (``python -m backend.tools.threshold_calibration``) showed that
cosine similarity cannot separate "same question reworded" from "same topic,
different question": the worst negative scored 0.904 while the median true
paraphrase scored 0.796.  These tests pin the consequence — a merely *related*
query must never be answered from cache without a model confirming that both
queries share one information need.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from backend import research_cache as rc_mod
from backend.research_cache import ResearchCache


# ── harness ────────────────────────────────────────────────────────────


class FakeCache:
    """In-memory stand-in for the Redis/memory cache singleton."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, ttl: int = 300):
        self.store[key] = value

    async def delete(self, key: str):
        self.store.pop(key, None)

    @staticmethod
    def hash_key(text: str) -> str:
        return f"h{abs(hash(text)) % 10**8}"


def _vector_for(cosine: float) -> tuple[list[float], list[float]]:
    """Two unit vectors whose cosine similarity is exactly *cosine*."""
    left = [1.0, 0.0]
    right = [cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine))]
    return left, right


@pytest.fixture
def cache_env(monkeypatch):
    """Isolated cache + controllable embeddings + controllable judge."""
    fake = FakeCache()
    monkeypatch.setattr(rc_mod, "cache", fake)

    state = {"cosine": 0.95, "judge": None, "judge_calls": 0, "embed_calls": 0,
             "candidate_text": "旧查询：读取大文件"}

    async def fake_embedding(_text, model=""):
        state["embed_calls"] += 1
        return _vector_for(state["cosine"])[0]

    monkeypatch.setattr(rc_mod, "get_embedding", fake_embedding)

    cache = ResearchCache()
    cache._confirm = True

    async def store_candidate(query: str, cosine: float):
        """Seed one cached result whose query sits at *cosine* from the probe."""
        state["cosine"] = cosine
        state["candidate_text"] = query
        # Point the probe vector at the doc vector for this cosine.
        _, doc = _vector_for(cosine)

        async def fixed_embedding(_text, model=""):
            state["embed_calls"] += 1
            return [1.0, 0.0]

        monkeypatch.setattr(rc_mod, "get_embedding", fixed_embedding)
        key = f"research:{FakeCache.hash_key(query)}"
        await fake.set(key, {"synthesis": f"{query} 的结果"})
        await fake.set(cache._embeddings_key, [{
            "key": key, "query": query, "embedding": doc, "stored_at": "now",
        }])
        # The probe must look like the other side of the pair.
        monkeypatch.setattr(rc_mod, "get_embedding", fake_embedding)
        state["embed_calls"] = 0
        return key

    def set_judge(answer):
        """Install an equivalence judge returning *answer* (or None to fail)."""
        state["judge"] = answer

        class _Stub:
            async def ainvoke(self, _messages):
                state["judge_calls"] += 1
                if answer is None:
                    raise RuntimeError("judge unavailable")

                class _Resp:
                    content = json.dumps({"same": answer}, ensure_ascii=False)
                return _Resp()

        import backend.models.provider as provider_mod
        monkeypatch.setattr(provider_mod, "create_chat_model", lambda *a, **k: _Stub())

    return {"cache": cache, "fake": fake, "state": state,
            "store_candidate": store_candidate, "set_judge": set_judge}


def _probe(cache_env, query: str):
    return asyncio.run(cache_env["cache"].get_similar(query))


# ── layer 1: exact match ───────────────────────────────────────────────


def test_exact_query_is_served_from_layer_one(cache_env):
    asyncio.run(cache_env["fake"].set(
        f"research:{FakeCache.hash_key('同一个问题')}", {"synthesis": "缓存结果"}
    ))
    assert _probe(cache_env, "同一个问题")["synthesis"] == "缓存结果"
    assert cache_env["state"]["judge_calls"] == 0


# ── layer 2 stage 1: near-duplicate ────────────────────────────────────


def test_near_duplicate_is_served_without_a_judge_call(cache_env):
    asyncio.run(cache_env["store_candidate"]("2024年新能源汽车销量", 0.95))
    # Install a judge that would fail loudly if it were consulted at all.
    cache_env["set_judge"](None)

    result = _probe(cache_env, "2024年新能源汽车销量是多少")

    assert result is not None
    assert cache_env["state"]["judge_calls"] == 0


def test_near_duplicate_boundary_is_the_configured_threshold(cache_env):
    """0.92 is the measured "above every negative" line."""
    assert rc_mod.RESEARCH_CACHE_THRESHOLD == 0.92

    asyncio.run(cache_env["store_candidate"]("查询 A", 0.919))
    cache_env["set_judge"](False)
    assert _probe(cache_env, "查询 B") is None          # just below → judged
    assert cache_env["state"]["judge_calls"] == 1


# ── layer 2 stage 2: the finding this whole exercise is about ──────────


def test_related_but_different_question_is_rejected(cache_env):
    """The measured killer pair: 读取大文件 vs 写入大文件 (cosine 0.904)."""
    asyncio.run(cache_env["store_candidate"]("Python 怎么高效读取大文件", 0.904))
    cache_env["set_judge"](False)

    assert _probe(cache_env, "Python 怎么高效写入大文件") is None
    assert cache_env["state"]["judge_calls"] == 1


def test_related_and_equivalent_question_is_served_after_confirmation(cache_env):
    asyncio.run(cache_env["store_candidate"]("去年新能源汽车销量数据", 0.796))
    cache_env["set_judge"](True)

    result = _probe(cache_env, "2024年新能源汽车销量是多少")

    assert result is not None
    assert "去年新能源汽车销量数据" in result["synthesis"]
    assert cache_env["state"]["judge_calls"] == 1


def test_judge_failure_fails_closed(cache_env):
    """An unavailable judge must never turn into a silent wrong hit."""
    asyncio.run(cache_env["store_candidate"]("某个旧查询", 0.85))
    cache_env["set_judge"](None)

    assert _probe(cache_env, "另一个相关查询") is None
    assert cache_env["state"]["judge_calls"] == 1


def test_below_the_floor_is_a_miss_without_calling_the_judge(cache_env):
    asyncio.run(cache_env["store_candidate"]("完全无关的话题", 0.28))
    cache_env["set_judge"](True)

    assert _probe(cache_env, "某查询") is None
    assert cache_env["state"]["judge_calls"] == 0


def test_floor_boundary_triggers_confirmation(cache_env):
    assert rc_mod.RESEARCH_CACHE_SIMILARITY_FLOOR == 0.40

    asyncio.run(cache_env["store_candidate"]("边界查询", 0.40))
    cache_env["set_judge"](True)
    assert _probe(cache_env, "探针查询") is not None
    assert cache_env["state"]["judge_calls"] == 1


def test_confirmation_can_be_switched_off(cache_env):
    asyncio.run(cache_env["store_candidate"]("相关查询", 0.85))
    cache_env["cache"]._confirm = False
    cache_env["set_judge"](True)

    assert _probe(cache_env, "另一个查询") is None
    assert cache_env["state"]["judge_calls"] == 0


# ── gaps and hygiene ───────────────────────────────────────────────────


def test_miss_when_no_candidates_are_stored(cache_env):
    assert _probe(cache_env, "任何查询") is None
    assert cache_env["state"]["judge_calls"] == 0


def test_empty_query_and_missing_embedding_are_misses(cache_env):
    assert _probe(cache_env, "") is None

    async def no_embedding(_text, model=""):
        return None

    rc_mod.get_embedding, original = no_embedding, rc_mod.get_embedding
    try:
        assert _probe(cache_env, "查询") is None
    finally:
        rc_mod.get_embedding = original


def test_dimension_mismatch_is_ignored(cache_env):
    async def wrong_dim(_text, model=""):
        return [1.0, 0.0, 0.0]

    rc_mod.get_embedding, original = wrong_dim, rc_mod.get_embedding
    try:
        asyncio.run(cache_env["fake"].set(cache_env["cache"]._embeddings_key, [{
            "key": "research:x", "query": "旧", "embedding": [1.0, 0.0],
        }]))
        assert _probe(cache_env, "新查询") is None
    finally:
        rc_mod.get_embedding = original


def test_same_information_need_short_circuits_identical_text(cache_env):
    """Comparing a query with itself must not spend a model call."""
    cache_env["set_judge"](True)
    same = asyncio.run(
        cache_env["cache"]._same_information_need("同一个查询", "同一个查询")
    )
    assert same is True
    assert cache_env["state"]["judge_calls"] == 0
