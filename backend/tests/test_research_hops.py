"""Tests for multi-hop (iterative) research.

The search backend is stubbed, so these tests exercise the hop control flow,
source de-duplication and numbering, gap analysis and citation plumbing without
touching the network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.agents import builtin_workers as bw


# ── stubs ──────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _ScriptedLLM:
    """Returns queued replies; records every prompt for assertions."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        if self._replies:
            return _Resp(self._replies.pop(0))
        return _Resp("资料中未找到")


def _scrape_result(title: str, url: str, text: str = None, structured=None) -> dict:
    return {
        "synthesis": text if text is not None else f"{title} 的正文。" * 20,
        "sources": [{"title": title, "url": url}],
        "structured_data": structured,
        "injection_findings": [],
    }


@pytest.fixture
def research_env(monkeypatch):
    """Patch the search backend, the semantic cache and pin a hop budget."""
    calls: list[str] = []
    responses: dict[str, dict] = {}
    default = _scrape_result("默认来源", "https://example.com/default")

    async def fake_search(query, llm, max_pages=3):
        calls.append(query)
        return responses.get(query, default)

    monkeypatch.setattr(bw, "search_and_scrape", fake_search)
    monkeypatch.setenv("AGENT_RESEARCH_MAX_HOPS", "2")
    monkeypatch.setenv("AGENT_RESEARCH_QUERIES_PER_HOP", "1")
    monkeypatch.setenv("AGENT_RESEARCH_MAX_SOURCES", "10")

    # Only the *search* provider type is stubbed: the embedding client also
    # resolves providers, so everything else must keep the real lookup.
    import backend.config as config_mod

    real_get_provider = config_mod.get_provider_by_type
    monkeypatch.setattr(
        config_mod,
        "get_provider_by_type",
        lambda type_=None, *a, **k: (object() if type_ == "search"
                                     else real_get_provider(type_, *a, **k)),
    )

    # Keep embeddings and ChromaDB out of these control-flow tests.
    import backend.research_cache as cache_mod

    class _NullCache:
        async def get_similar(self, _text):
            return None

        async def store(self, *_a, **_k):
            return None

    monkeypatch.setattr(cache_mod, "get_research_cache", lambda: _NullCache())
    monkeypatch.setattr(cache_mod, "get_knowledge_base", lambda: None)

    return {"calls": calls, "responses": responses}


def _run_worker(monkeypatch, llm, prompt="研究一下新能源汽车销量"):
    monkeypatch.setattr(bw, "_get_chat_llm", lambda _state: llm)
    state = {
        "tasks": [{"agent": "research", "prompt": prompt, "id": "research:1"}],
        "user_request": prompt,
        "run_id": "run_test",
        "chat_model_id": "test",
    }
    return asyncio.run(bw.research_worker(state))["research_results"][0]


# ── unit level ─────────────────────────────────────────────────────────


def test_source_catalogue_numbers_and_caps_sources():
    sources = [{"title": f"T{i}", "url": f"https://x/{i}"} for i in range(5)]
    catalogue = bw._source_catalogue(sources, limit=3)

    assert [c["index"] for c in catalogue] == [1, 2, 3]
    assert catalogue[0]["url"] == "https://x/0"

    rendered = bw._render_catalogue(catalogue)
    assert "[1] T0" in rendered and "[3] T2" in rendered
    assert bw._render_catalogue([]) == "（无可用来源）"


def test_source_catalogue_falls_back_when_title_is_missing():
    catalogue = bw._source_catalogue([{"url": "https://x"}], limit=1)
    assert catalogue[0]["title"] == "来源 1"


def test_gap_analysis_parses_json():
    async def scenario():
        llm = _ScriptedLLM([json.dumps({
            "sufficient": False,
            "missing": ["缺少分项数据"],
            "follow_up_queries": ["新能源汽车 分车型 销量"],
        }, ensure_ascii=False)])
        return await bw._research_missing_gaps(llm, "任务", "已有内容", ["标题"])

    gap = asyncio.run(scenario())
    assert gap["sufficient"] is False
    assert gap["missing"] == ["缺少分项数据"]
    assert gap["follow_up_queries"] == ["新能源汽车 分车型 销量"]


def test_gap_analysis_fails_closed_on_bad_output():
    """A broken analysis must stop the loop, never extend it."""
    async def scenario(reply):
        return await bw._research_missing_gaps(_ScriptedLLM([reply]), "任务", "内容", [])

    assert asyncio.run(scenario("not json at all"))["sufficient"] is True
    assert asyncio.run(scenario(json.dumps(
        {"sufficient": False, "follow_up_queries": []}
    )))["sufficient"] is True
    assert asyncio.run(scenario(json.dumps(
        {"sufficient": True, "follow_up_queries": ["x"]}
    )))["sufficient"] is True


def test_gap_analysis_survives_llm_failure():
    class _Boom:
        async def ainvoke(self, _messages):
            raise RuntimeError("provider down")

    gap = asyncio.run(bw._research_missing_gaps(_Boom(), "t", "e", []))
    assert gap["sufficient"] is True
    assert gap["follow_up_queries"] == []


# ── hop control flow ───────────────────────────────────────────────────


def test_single_hop_when_research_is_already_sufficient(research_env, monkeypatch):
    research_env["responses"]["研究一下新能源汽车销量"] = _scrape_result(
        "销量报告", "https://a/1"
    )
    llm = _ScriptedLLM([
        json.dumps({"sufficient": True}),          # gap analysis on hop 1
        "结论 [1]。",                               # synthesis
    ])

    entry = _run_worker(monkeypatch, llm)

    assert research_env["calls"] == ["研究一下新能源汽车销量"]
    assert entry["research_hops"] == 1
    assert entry["hop_reports"][0]["sufficient"] is True
    assert len(entry["sources"]) == 1


def test_second_hop_runs_when_a_gap_is_found(research_env, monkeypatch):
    research_env["responses"]["研究一下新能源汽车销量"] = _scrape_result(
        "销量报告", "https://a/1"
    )
    research_env["responses"]["新能源汽车 分车型 销量"] = _scrape_result(
        "分车型数据", "https://b/2"
    )
    llm = _ScriptedLLM([
        json.dumps({"sufficient": False, "missing": ["缺少分项数据"],
                    "follow_up_queries": ["新能源汽车 分车型 销量"]}, ensure_ascii=False),
        json.dumps({"sufficient": True}),          # hop 2 gap analysis
        "结论 [1][2]。",
    ])

    entry = _run_worker(monkeypatch, llm)

    assert research_env["calls"] == ["研究一下新能源汽车销量", "新能源汽车 分车型 销量"]
    assert entry["research_hops"] == 2
    assert [s["index"] for s in entry["sources"]] == [1, 2]
    assert entry["knowledge_gaps"] == ["缺少分项数据"]
    assert [h["new_sources"] for h in entry["hop_reports"]] == [1, 1]


def test_hop_count_is_bounded_by_the_budget(research_env, monkeypatch):
    monkeypatch.setenv("AGENT_RESEARCH_MAX_HOPS", "1")
    research_env["responses"]["研究一下新能源汽车销量"] = _scrape_result("A", "https://a/1")
    research_env["responses"]["追问"] = _scrape_result("B", "https://b/2")
    llm = _ScriptedLLM([
        json.dumps({"sufficient": False, "missing": ["x"],
                    "follow_up_queries": ["追问"]}, ensure_ascii=False),
        "结论 [1][2]。",
    ])

    entry = _run_worker(monkeypatch, llm)

    assert len(research_env["calls"]) == 2          # 1 hop of follow-up only
    assert entry["research_hops"] == 2             # seed hop + 1 follow-up


def test_zero_hops_disables_follow_up_entirely(research_env, monkeypatch):
    monkeypatch.setenv("AGENT_RESEARCH_MAX_HOPS", "0")
    llm = _ScriptedLLM(["结论 [1]。"])

    entry = _run_worker(monkeypatch, llm)

    assert len(research_env["calls"]) == 1
    assert entry["research_hops"] == 1
    assert entry["hop_reports"] == []
    # No gap analysis call should have been made at all.
    assert all("多轮资料搜集" not in p for p in llm.prompts)


def test_duplicate_urls_are_deduplicated_across_hops(research_env, monkeypatch):
    same = _scrape_result("同一篇", "https://same/1")
    research_env["responses"]["研究一下新能源汽车销量"] = same
    research_env["responses"]["追问"] = same
    llm = _ScriptedLLM([
        json.dumps({"sufficient": False, "missing": ["x"],
                    "follow_up_queries": ["追问"]}, ensure_ascii=False),
        "结论 [1]。",
    ])

    entry = _run_worker(monkeypatch, llm)

    assert len(entry["sources"]) == 1
    assert entry["hop_reports"][0]["new_sources"] == 1
    assert entry["hop_reports"][1]["new_sources"] == 0     # nothing new


def test_empty_first_hop_broadens_instead_of_repeating_the_query(
    research_env, monkeypatch
):
    """An unproductive hop must not just re-run the identical search."""
    research_env["responses"]["研究一下新能源汽车销量"] = {
        "synthesis": "", "sources": [], "structured_data": None,
        "injection_findings": [],
    }
    research_env["responses"]["新能源汽车 月度销量 2024"] = _scrape_result(
        "月度销量", "https://b/2"
    )
    llm = _ScriptedLLM([
        "新能源汽车 月度销量 2024\n新能源汽车 分车型销量",   # broadening keywords
        json.dumps({"sufficient": True}),                   # gap analysis after hop 2
        "结论 [1]。",
    ])

    entry = _run_worker(monkeypatch, llm)

    assert research_env["calls"] == ["研究一下新能源汽车销量", "新能源汽车 月度销量 2024"]
    assert len(entry["sources"]) == 1
    assert entry["sources"][0]["url"] == "https://b/2"


def test_broadening_ignores_a_json_reply_from_the_query_model(
    research_env, monkeypatch
):
    """A malformed keyword reply must never be pasted in as a search query."""
    research_env["responses"]["研究一下新能源汽车销量"] = {
        "synthesis": "", "sources": [], "structured_data": None,
        "injection_findings": [],
    }
    llm = _ScriptedLLM([
        json.dumps({"sufficient": False, "follow_up_queries": ["追问来源"]}),
        "结论 [1]。",
    ])

    entry = _run_worker(monkeypatch, llm)

    # The JSON reply was rejected, and re-issuing the original query is
    # pointless, so the loop ends after a single search round.
    assert research_env["calls"] == ["研究一下新能源汽车销量"]
    assert entry["sources"] == []
    assert entry["research_hops"] == 1
    assert not any("sufficient" in call for call in research_env["calls"])


# ── citations & safety plumbing ────────────────────────────────────────


def test_synthesis_prompt_receives_the_numbered_catalogue(research_env, monkeypatch):
    research_env["responses"]["研究一下新能源汽车销量"] = _scrape_result(
        "销量报告", "https://a/1"
    )
    llm = _ScriptedLLM([json.dumps({"sufficient": True}), "结论 [1]。"])
    _run_worker(monkeypatch, llm)

    synthesis_prompt = next(p for p in llm.prompts if "可用来源清单" in p)
    assert "[1] 销量报告 — https://a/1" in synthesis_prompt
    assert "研究一下新能源汽车销量" in synthesis_prompt
    assert "只能使用清单里出现过的编号" in synthesis_prompt


def test_external_text_is_fenced_before_synthesis(research_env, monkeypatch):
    research_env["responses"]["研究一下新能源汽车销量"] = _scrape_result(
        "销量报告", "https://a/1"
    )
    llm = _ScriptedLLM([json.dumps({"sufficient": True}), "结论 [1]。"])
    _run_worker(monkeypatch, llm)

    synthesis_prompt = next(p for p in llm.prompts if "可用来源清单" in p)
    assert "<<<UNTRUSTED_EXTERNAL_CONTENT>>>" in synthesis_prompt
    assert "<<<END_UNTRUSTED_EXTERNAL_CONTENT>>>" in synthesis_prompt


def test_structured_data_from_search_is_preserved(research_env, monkeypatch):
    payload = {"viable": True, "chart_type": "bar", "labels": ["a"],
               "datasets": [{"label": "n", "values": [1]}]}
    research_env["responses"]["研究一下新能源汽车销量"] = _scrape_result(
        "销量报告", "https://a/1", structured=payload
    )
    llm = _ScriptedLLM([json.dumps({"sufficient": True}), "结论 [1]。"])

    entry = _run_worker(monkeypatch, llm)
    assert entry["structured_data"] == payload


def test_injection_findings_are_hoisted_to_the_entry(research_env, monkeypatch):
    flagged = _scrape_result("恶意页", "https://evil/1")
    flagged["injection_findings"] = [{"id": "injection.override.en", "severity": "critical",
                                      "message": "忽略指令", "evidence": "ignore all"}]
    research_env["responses"]["研究一下新能源汽车销量"] = flagged
    llm = _ScriptedLLM([json.dumps({"sufficient": True}), "结论 [1]。"])

    entry = _run_worker(monkeypatch, llm)
    assert entry["safety_findings"][0]["id"] == "injection.override.en"


def test_research_failure_is_reported_as_an_error_entry(research_env, monkeypatch):
    async def _boom(_query, _llm, max_pages=3):
        raise RuntimeError("search backend down")

    monkeypatch.setattr(bw, "search_and_scrape", _boom)
    llm = _ScriptedLLM([])

    entry = _run_worker(monkeypatch, llm)
    assert "search backend down" in entry["error"]


def test_no_research_task_returns_an_empty_result(monkeypatch):
    state = {"tasks": [{"agent": "analyst", "prompt": "x"}]}
    assert asyncio.run(bw.research_worker(state)) == {"research_results": []}
