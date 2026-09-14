"""Tests for cross-session long-term memory.

The trust split is the important part: facts and preferences are written and
recalled automatically, while reusable conclusions must stay pending until a
human confirms them.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import memory as memory_mod
from backend.memory import (
    ACTIVE,
    CONCLUSION,
    FACT,
    PENDING,
    REJECTED,
    PREFERENCE,
    MemoryStore,
    extract_memories,
    remember_run,
    token_overlap,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated store with deterministic (non-network) embeddings."""
    monkeypatch.setenv("AGENT_MEMORY", "1")
    monkeypatch.setenv("AGENT_MEMORY_TOP_K", "5")
    monkeypatch.setenv("AGENT_MEMORY_MIN_SCORE", "0.0")

    instance = MemoryStore(path=tmp_path / "memory" / "memories.json")

    async def _no_embedding(_text):
        return None                      # forces the token-overlap fallback

    monkeypatch.setattr(instance, "_embed", _no_embedding)
    monkeypatch.setattr(memory_mod, "_store", instance)
    return instance


# ── kind / status semantics ────────────────────────────────────────────


def test_facts_and_preferences_are_active_immediately(store):
    fact = asyncio.run(store.add("我在做 CanDoIt 多智能体平台", kind=FACT))
    pref = asyncio.run(store.add("报告一律用中文", kind=PREFERENCE))

    assert fact.status == ACTIVE
    assert pref.status == ACTIVE
    assert store.stats()["by_status"][ACTIVE] == 2


def test_conclusions_are_staged_as_pending(store):
    item = asyncio.run(store.add("2024 年新能源汽车销量约 1286 万辆", kind=CONCLUSION))

    assert item.status == PENDING
    assert store.stats()["pending"] == 1
    assert store.stats()["by_status"][PENDING] == 1


def test_pending_conclusions_are_not_recalled_until_confirmed(store):
    item = asyncio.run(store.add("2024 年新能源汽车销量约 1286 万辆", kind=CONCLUSION))
    asyncio.run(store.add("用户在做新能源汽车相关的研究", kind=FACT))

    recalled = asyncio.run(store.recall("新能源汽车销量"))
    assert all(entry["id"] != item.id for entry in recalled)
    assert recalled, "active facts must still be recallable"

    store.confirm(item.id)
    recalled = asyncio.run(store.recall("新能源汽车销量是多少"))
    assert any(entry["id"] == item.id for entry in recalled)
    assert store.get(item.id).status == ACTIVE


def test_rejected_memories_are_never_recalled(store):
    item = asyncio.run(store.add("用户喜欢极简风格", kind=PREFERENCE))
    asyncio.run(store.recall("风格"))              # sanity: recallable first
    store.reject(item.id)

    assert store.get(item.id).status == REJECTED
    assert asyncio.run(store.recall("风格")) == []


def test_unknown_kinds_fall_back_to_fact(store):
    item = asyncio.run(store.add("随便什么", kind="nonsense"))
    assert item.kind == FACT
    assert item.status == ACTIVE


def test_explicit_status_overrides_the_kind_default(store):
    item = asyncio.run(store.add("人工添加的结论", kind=CONCLUSION, status=ACTIVE))
    assert item.status == ACTIVE


# ── safety ─────────────────────────────────────────────────────────────


def test_credentials_are_never_stored(store):
    assert asyncio.run(store.add("我的 key 是 sk-abcdefghijklmnopqrstuvwxyz")) is None
    assert asyncio.run(store.add("密码：hunter2hunter2")) is None
    assert store.stats()["total"] == 0


def test_empty_text_is_rejected(store):
    assert asyncio.run(store.add("   ")) is None
    assert store.stats()["total"] == 0


# ── dedupe / limits ────────────────────────────────────────────────────


def test_identical_memories_are_refreshed_not_duplicated(store):
    first = asyncio.run(store.add("报告一律用中文", kind=PREFERENCE))
    second = asyncio.run(store.add("报告一律用中文  ", kind=PREFERENCE))

    assert first.id == second.id
    assert store.stats()["total"] == 1


def test_store_is_trimmed_to_the_cap(store, monkeypatch):
    monkeypatch.setattr(memory_mod, "_MAX_ITEMS", 5)
    for index in range(8):
        asyncio.run(store.add(f"第 {index} 条记忆内容", kind=FACT))
    assert store.stats()["total"] == 5


def test_persistence_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "memories.json"
    monkeypatch.setenv("AGENT_MEMORY", "1")

    first = MemoryStore(path=path)

    async def _no_embedding(_text):
        return None

    monkeypatch.setattr(first, "_embed", _no_embedding)
    asyncio.run(first.add("用户的项目叫 CanDoIt", kind=FACT))
    asyncio.run(first.add("用户的结论", kind=CONCLUSION))

    second = MemoryStore(path=path)
    items = second.items()
    assert {item.text for item in items} == {"用户的项目叫 CanDoIt", "用户的结论"}
    assert second.stats()["pending"] == 1


def test_delete_removes_a_memory(store):
    item = asyncio.run(store.add("临时事实", kind=FACT))
    assert store.delete(item.id) is True
    assert store.delete(item.id) is False
    assert store.get(item.id) is None


def test_corrupt_store_starts_empty(tmp_path, monkeypatch):
    path = tmp_path / "memories.json"
    path.write_text("{not json", encoding="utf-8")
    assert MemoryStore(path=path).items() == []


# ── recall ─────────────────────────────────────────────────────────────


def test_token_overlap_fallback_orders_by_relevance(store):
    asyncio.run(store.add("用户在做新能源汽车销量研究", kind=FACT))
    asyncio.run(store.add("用户喜欢深色模式", kind=PREFERENCE))

    recalled = asyncio.run(store.recall("新能源汽车销量数据"))
    assert recalled
    assert "新能源汽车" in recalled[0]["text"]
    assert recalled[0]["score"] > 0


def test_min_score_filters_weak_matches(store):
    monkeypatch = None
    asyncio.run(store.add("用户喜欢深色模式", kind=PREFERENCE))
    strong = asyncio.run(store.recall("深色模式", min_score=0.2))
    weak = asyncio.run(store.recall("完全无关的航天话题", min_score=0.9))
    assert strong and not weak


def test_recall_uses_embeddings_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_MIN_SCORE", "0.0")
    store = MemoryStore(path=tmp_path / "m.json")

    vectors = {
        "新能源汽车": [1.0, 0.0, 0.0],
        "烘焙蛋糕": [0.0, 1.0, 0.0],
    }

    async def _fake_embed(text):
        for key, vector in vectors.items():
            if key in text:
                return vector
        return [0.9, 0.1, 0.0]           # query vector: close to 新能源汽车

    monkeypatch.setattr(store, "_embed", _fake_embed)
    monkeypatch.setattr(memory_mod, "_store", store)

    asyncio.run(store.add("新能源汽车行业数据", kind=FACT))
    asyncio.run(store.add("烘焙蛋糕配方", kind=FACT))

    recalled = asyncio.run(store.recall("新能源汽车", top_k=2))
    assert "新能源汽车" in recalled[0]["text"]
    assert recalled[0]["score"] > recalled[-1]["score"]


def test_recall_respects_top_k(store):
    for index in range(6):
        asyncio.run(store.add(f"关于新能源汽车的第 {index} 条事实", kind=FACT))
    assert len(asyncio.run(store.recall("新能源汽车", top_k=2))) == 2
    assert asyncio.run(store.recall("新能源汽车", top_k=0)) == []


def test_recall_is_empty_for_an_empty_query(store):
    asyncio.run(store.add("某条事实", kind=FACT))
    assert asyncio.run(store.recall("   ")) == []


def test_recall_can_be_disabled(store, monkeypatch):
    asyncio.run(store.add("用户喜欢中文报告", kind=PREFERENCE))
    monkeypatch.setenv("AGENT_MEMORY", "0")
    block, items = asyncio.run(store.recall_for_prompt("报告"))
    assert block == "" and items == []


def test_recall_for_prompt_renders_a_memory_block(store):
    asyncio.run(store.add("报告一律用中文", kind=PREFERENCE))
    block, items = asyncio.run(store.recall_for_prompt("帮我写一份报告"))
    assert items
    assert "报告一律用中文" in block
    assert "跨会话记忆" in block


# ── extraction ─────────────────────────────────────────────────────────


class _ScriptedLLM:
    def __init__(self, reply: str):
        self.reply = reply

    async def ainvoke(self, _messages):
        class _Resp:
            content = self.reply
        return _Resp()


def _patch_llm(monkeypatch, reply):
    import backend.models.provider as provider_mod

    monkeypatch.setattr(provider_mod, "create_chat_model",
                        lambda *a, **k: _ScriptedLLM(reply))


def test_extract_memories_parses_valid_items(monkeypatch, store):
    _patch_llm(monkeypatch, json.dumps([
        {"kind": "preference", "text": "用户要求报告用中文", "confidence": 0.9},
        {"kind": "conclusion", "text": "2024 年销量 1286 万辆", "confidence": 0.7},
    ], ensure_ascii=False))

    items = asyncio.run(extract_memories(user_request="x", final_response="y"))
    assert [i["kind"] for i in items] == ["preference", "conclusion"]
    assert items[0]["confidence"] == pytest.approx(0.9)


def test_extract_memories_drops_junk_and_bad_kinds(monkeypatch, store):
    _patch_llm(monkeypatch, json.dumps([
        {"kind": "nonsense", "text": "未知类型"},
        {"kind": "fact", "text": "ab"},                       # too short
        {"kind": "fact", "text": ""},
        {"kind": "fact", "text": "用户的项目叫 CanDoIt"},
    ], ensure_ascii=False))

    items = asyncio.run(extract_memories(user_request="x"))
    assert [i["text"] for i in items] == ["用户的项目叫 CanDoIt"]


def test_extract_memories_handles_a_non_json_reply(monkeypatch, store):
    _patch_llm(monkeypatch, "我觉得没什么好记的")
    assert asyncio.run(extract_memories(user_request="x")) == []


def test_extract_memories_survives_an_llm_failure(monkeypatch, store):
    import backend.models.provider as provider_mod

    def _boom(*_a, **_k):
        raise RuntimeError("no provider")

    monkeypatch.setattr(provider_mod, "create_chat_model", _boom)
    assert asyncio.run(extract_memories(user_request="x")) == []


def test_remember_run_splits_stored_and_pending(monkeypatch, store):
    _patch_llm(monkeypatch, json.dumps([
        {"kind": "fact", "text": "用户在做多智能体平台", "confidence": 0.9},
        {"kind": "conclusion", "text": "2024 年新能源汽车销量 1286 万辆", "confidence": 0.8},
    ], ensure_ascii=False))

    result = asyncio.run(remember_run(
        user_request="研究新能源汽车", final_response="结论如下",
        state={"run_id": "run_1", "session_id": "s1"},
    ))

    assert len(result["stored"]) == 1 and result["stored"][0]["kind"] == FACT
    assert len(result["pending"]) == 1 and result["pending"][0]["kind"] == CONCLUSION
    assert store.stats()["by_status"][ACTIVE] == 1
    assert store.stats()["by_status"][PENDING] == 1


def test_remember_run_is_a_noop_when_disabled(monkeypatch, store):
    monkeypatch.setenv("AGENT_MEMORY", "0")
    result = asyncio.run(remember_run(user_request="x", final_response="y"))
    assert result["stored"] == [] and result["pending"] == []
    assert "关闭" in result["skipped"]


def test_memories_written_by_a_run_carry_their_origin(monkeypatch, store):
    _patch_llm(monkeypatch, json.dumps([
        {"kind": "fact", "text": "用户的项目叫 CanDoIt", "confidence": 1.0},
    ], ensure_ascii=False))

    asyncio.run(remember_run(
        user_request="x", final_response="y",
        state={"run_id": "run_42", "session_id": "sess_7"},
    ))
    item = store.items()[0]
    assert item.run_id == "run_42"
    assert item.session_id == "sess_7"


# ── API ────────────────────────────────────────────────────────────────


@pytest.fixture
def client(store):
    from fastapi.testclient import TestClient

    from backend.main import app

    return TestClient(app)


def test_memory_api_lifecycle(client):
    created = client.post("/api/memory", json={
        "text": "报告一律用中文", "kind": "preference",
    })
    assert created.status_code == 200
    memory_id = created.json()["memory"]["id"]

    listed = client.get("/api/memory").json()
    assert listed["count"] == 1
    assert listed["memories"][0]["kind"] == PREFERENCE

    assert client.post(f"/api/memory/{memory_id}/reject").status_code == 200
    assert client.get("/api/memory").json()["memories"][0]["status"] == REJECTED

    assert client.delete(f"/api/memory/{memory_id}").status_code == 200
    assert client.get("/api/memory").json()["count"] == 0


def test_memory_api_confirm_promotes_a_pending_conclusion(client):
    """A manually added memory is human-authored, so it starts active; only
    model-extracted conclusions need confirmation.  An explicit ``status``
    is still honoured.
    """
    created = client.post("/api/memory", json={
        "text": "2024 年新能源汽车销量 1286 万辆", "kind": "conclusion",
    }).json()
    assert created["memory"]["status"] == ACTIVE

    staged = client.post("/api/memory", json={
        "text": "2025 年新能源汽车销量预测 1600 万辆", "kind": "conclusion",
        "status": "pending",
    }).json()
    memory_id = staged["memory"]["id"]
    assert staged["memory"]["status"] == PENDING

    assert client.get("/api/memory/stats").json()["pending"] == 1
    assert client.get("/api/memory/pending/count").json()["pending"] == 1
    assert client.post(f"/api/memory/{memory_id}/confirm").json()["memory"]["status"] == ACTIVE
    assert client.get("/api/memory/stats").json()["pending"] == 0


def test_memory_api_search_and_filters(client):
    client.post("/api/memory", json={"text": "用户在做新能源汽车研究", "kind": "fact"})
    client.post("/api/memory", json={"text": "用户喜欢深色模式", "kind": "preference"})

    found = client.post("/api/memory/search", json={"query": "新能源汽车", "top_k": 5}).json()
    assert found["count"] >= 1
    assert "新能源汽车" in found["results"][0]["text"]

    only_prefs = client.get("/api/memory", params={"kind": "preference"}).json()
    assert only_prefs["count"] == 1


def test_memory_api_rejects_unknown_kind_and_missing_ids(client):
    assert client.post("/api/memory", json={"text": "x", "kind": "nonsense"}).status_code == 400
    assert client.post("/api/memory/nope/confirm").status_code == 404
    assert client.delete("/api/memory/nope").status_code == 404
