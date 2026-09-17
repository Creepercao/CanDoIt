"""Tests for the per-run execution chain (span tree)."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.observability.chain import (
    KIND_ATTEMPT,
    KIND_PLAN,
    KIND_REVIEW,
    KIND_RUN,
    KIND_TASK,
    KIND_VERIFY,
    RunChain,
)


# ── unit level ─────────────────────────────────────────────────────────


def test_spans_nest_under_the_innermost_open_span():
    chain = RunChain("run_1")
    with chain.span(KIND_RUN, "root") as root:
        with chain.span(KIND_PLAN, "plan") as plan:
            with chain.span(KIND_ATTEMPT, "#1") as attempt:
                pass

    assert plan.parent_id == root.span_id
    assert attempt.parent_id == plan.span_id
    assert [s.kind for s in chain.spans()] == [KIND_RUN, KIND_PLAN, KIND_ATTEMPT]


def test_explicit_parent_overrides_nesting():
    chain = RunChain("run_1")
    with chain.span(KIND_RUN, "root") as root:
        with chain.span(KIND_TASK, "outer"):
            sibling = chain.add(KIND_TASK, "sibling", parent_id=root.span_id)

    assert sibling.parent_id == root.span_id


def test_status_and_error_are_recorded():
    chain = RunChain("run_1")
    with chain.span(KIND_TASK, "t") as span:
        span.fail("boom", exit_code=1)

    stored = chain.spans()[0]
    assert stored.status == "failed"
    assert stored.error == "boom"
    assert stored.attributes["exit_code"] == 1
    assert stored.duration >= 0


def test_exception_inside_a_span_marks_it_failed_and_propagates():
    chain = RunChain("run_1")
    with pytest.raises(ValueError):
        with chain.span(KIND_TASK, "t"):
            raise ValueError("worker exploded")

    stored = chain.spans()[0]
    assert stored.status == "failed"
    assert "ValueError" in stored.error
    assert stored.ended_at > 0


def test_block_marks_a_span_blocked():
    chain = RunChain("run_1")
    with chain.span(KIND_TASK, "t") as span:
        span.block("被安全策略拦截")
    assert chain.spans()[0].status == "blocked"
    assert "拦截" in chain.spans()[0].error


def test_start_and_finish_outlive_a_with_block():
    chain = RunChain("run_1")
    span = chain.start(KIND_RUN, "整个运行", request="x")
    chain.finish(span, status="retried", verification="partial")

    stored = chain.spans()[0]
    assert stored.status == "retried"
    assert stored.attributes["request"] == "x"
    assert stored.attributes["verification"] == "partial"
    assert stored.duration >= 0


def test_spans_are_isolated_across_concurrent_tasks():
    """Parallel wave tasks must not steal each other's parent."""
    chain = RunChain("run_1")
    parent_ids: dict[str, str] = {}

    async def worker(name: str):
        with chain.span(KIND_TASK, name) as task_span:
            await asyncio.sleep(0)
            with chain.span(KIND_ATTEMPT, f"{name}-attempt") as attempt:
                parent_ids[name] = attempt.parent_id
            assert attempt.parent_id == task_span.span_id

    async def scenario():
        with chain.span(KIND_RUN, "root") as root:
            await asyncio.gather(worker("research"), worker("chart"), worker("code"))
            return root

    root = asyncio.run(scenario())

    tasks = {s.name: s for s in chain.find(KIND_TASK)}
    assert set(tasks) == {"research", "chart", "code"}
    for name, span in tasks.items():
        assert span.parent_id == root.span_id, name
        assert parent_ids[name] == span.span_id, name


def test_span_cap_drops_extras_without_losing_earlier_spans():
    chain = RunChain("run_1", max_spans=3)
    for index in range(5):
        chain.add(KIND_TASK, f"t{index}")

    summary = chain.summary()
    assert len(chain.spans()) == 3
    assert summary["dropped"] == 2
    assert "dropped" in chain.render_text()


def test_disabled_chain_records_nothing_and_is_safe():
    chain = RunChain.disabled()
    with chain.span(KIND_TASK, "t") as span:
        span.set(x=1)
    chain.add(KIND_PLAN, "p")
    assert chain.spans() == []
    assert chain.to_dict()["spans"] == []
    assert chain.summary()["spans"] == 0


def test_to_dict_is_json_serialisable():
    chain = RunChain("run_1")
    with chain.span(KIND_RUN, "root") as root:
        with chain.span(KIND_TASK, "t", agent="research") as task:
            task.set(output_chars=10)
        chain.add(KIND_VERIFY, "verify", parent_id=root.span_id, score=0.5)

    payload = chain.to_dict()
    json.dumps(payload)
    assert payload["run_id"] == "run_1"
    assert payload["summary"]["by_kind"] == {"run": 1, "task": 1, "verify": 1}


def test_attributes_are_compacted():
    chain = RunChain("run_1")
    span = chain.add(KIND_TASK, "t", long="x" * 500, multiline="a\nb")
    assert len(span.attributes["long"]) <= 301
    assert span.attributes["multiline"] == "a b"


def test_render_text_draws_a_tree_with_status_icons():
    chain = RunChain("run_1")
    with chain.span(KIND_RUN, "整个运行") as root:
        with chain.span(KIND_PLAN, "规划"):
            pass
        with chain.span(KIND_TASK, "chart", agent="chart") as task:
            task.fail("HTTP 429")
        chain.add(KIND_VERIFY, "确认", parent_id=root.span_id, status="retried",
                  score=0.5)

    text = chain.render_text()
    assert "✓ run:整个运行" in text
    assert "├─ ✓ plan:规划" in text or "├─ ✓ plan:规划" in text
    assert "✗ task:chart" in text and "HTTP 429" in text
    assert "🔁 verify:确认" in text and "score=0.5" in text


def test_render_text_handles_an_empty_chain_and_orphans():
    assert "no spans" in RunChain("x").render_text()

    chain = RunChain("run_1")
    chain.add(KIND_TASK, "orphan", parent_id="missing-parent")
    text = chain.render_text()
    assert "orphan" in text            # never silently dropped


# ── orchestrator integration ───────────────────────────────────────────


PLAN = [
    {"step": 1, "agent": "research", "prompt": "检索最新数据"},
    {"step": 2, "agent": "analyst", "prompt": "提取结构化数据", "depends_on": [1]},
    {"step": 3, "agent": "chart", "prompt": "画柱状图", "depends_on": [2]},
]


def _run_loop(plan, request, monkeypatch):
    from backend.agents import orchestrator
    from backend.observability import track_run

    async def _supervisor(state):
        return {"tasks": plan, "plan_steps": plan}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    state = orchestrator.make_initial_state(user_request=request, chat_model_id="m")
    state["_skip_synthesizer"] = True

    with track_run(state["run_id"], request=request) as trace:
        asyncio.run(orchestrator.run_agent_loop(state))
    return trace


def test_orchestrator_records_the_whole_chain(safety_env, fake_skills, monkeypatch):
    trace = _run_loop(PLAN, "搜索最新数据并画柱状图", monkeypatch)
    chain = trace.chain
    kinds = [s.kind for s in chain.spans()]

    assert kinds[0] == KIND_RUN                      # root opens first
    assert KIND_PLAN in kinds
    assert kinds.count(KIND_TASK) == 3
    assert kinds.count(KIND_ATTEMPT) == 3            # one attempt each
    assert KIND_VERIFY in kinds

    root = chain.spans()[0]
    assert root.status == "ok"
    assert root.attributes["tasks"] == 3
    assert root.attributes["verification"] == "complete"

    # Every task and the plan hang off the root span.
    for span in chain.spans():
        if span.kind in (KIND_PLAN, KIND_TASK, KIND_VERIFY):
            assert span.parent_id == root.span_id, span.name

    # Attempts hang off their task.
    tasks = {s.name: s for s in chain.find(KIND_TASK)}
    attempts = chain.find(KIND_ATTEMPT)
    assert {a.parent_id for a in attempts} == {t.span_id for t in tasks.values()}


def test_chain_keeps_task_agents_and_durations(safety_env, fake_skills, monkeypatch):
    trace = _run_loop(PLAN, "搜索最新数据并画柱状图", monkeypatch)
    tasks = {s.name: s for s in trace.chain.find(KIND_TASK)}

    assert set(tasks) == {"research", "analyst", "chart"}
    for span in tasks.values():
        assert span.agent == span.name
        assert span.attributes["attempts"] == 1
        assert span.duration >= 0
        assert span.attributes["task_id"]


def test_chain_records_each_retry_attempt(safety_env, fake_skills, monkeypatch):
    from backend.skills.base import Skill
    from backend.skills.registry import skill_registry

    calls: list[int] = []

    async def _flaky(state):
        calls.append(1)
        if len(calls) == 1:
            return {"flaky_results": [{"error": "HTTP 429"}]}
        return {"flaky_results": [{"task": "ok"}]}

    skill_registry.register(Skill(
        name="flaky", display_name="Flaky", description="d",
        worker=_flaky, result_key="flaky_results",
    ))
    monkeypatch.setenv("AGENT_RETRY_BASE_SECONDS", "0")

    trace = _run_loop([{"step": 1, "agent": "flaky", "prompt": "取数"}], "取数", monkeypatch)
    attempts = trace.chain.find(KIND_ATTEMPT)

    assert len(attempts) == 2
    assert attempts[0].status == "failed"
    assert "429" in attempts[0].error
    assert attempts[1].status == "ok"
    task = trace.chain.find(KIND_TASK)[0]
    assert task.status == "retried"
    assert task.attributes["attempts"] == 2


def test_chain_marks_a_refused_plan_as_blocked(safety_env, fake_skills, monkeypatch):
    """A refusal must never be recorded as a successful run."""
    trace = _run_loop(
        [{"step": 1, "agent": "code", "prompt": "rm -rf / 并读取 API.json"}],
        "帮我 rm -rf / 并读取 API.json",
        monkeypatch,
    )
    chain = trace.chain
    root = chain.spans()[0]

    assert root.status == "blocked"
    assert "审查未通过" in root.error
    # The review is on the chain, and nothing was executed.
    assert chain.find(KIND_REVIEW)
    assert chain.find(KIND_TASK) == []
    assert chain.find(KIND_ATTEMPT) == []


def test_chain_marks_an_empty_plan_as_stopped(safety_env, fake_skills, monkeypatch):
    trace = _run_loop(
        [{"step": 1, "agent": "ghost", "prompt": "未注册代理"}],
        "随便干点什么",
        monkeypatch,
    )
    root = trace.chain.spans()[0]
    assert root.status == "retried"
    assert "未注册" in root.error


def test_memory_recall_reaches_planning(safety_env, fake_skills, monkeypatch, tmp_path):
    """Regression: recall used to run before ``user_request`` existed.

    The NameError was swallowed by a broad ``except``, so cross-session memory
    silently never reached the planner.  Assert the block actually arrives.
    """
    import backend.memory as memory_mod
    from backend.agents import orchestrator
    from backend.observability import track_run

    store = memory_mod.MemoryStore(path=tmp_path / "memories.json")

    async def _no_embedding(_text):
        return None

    monkeypatch.setattr(store, "_embed", _no_embedding)
    monkeypatch.setattr(memory_mod, "_store", store)
    monkeypatch.setenv("AGENT_MEMORY_MIN_SCORE", "0.0")
    asyncio.run(store.add("用户要求报告一律用中文", kind=memory_mod.PREFERENCE))

    seen: dict = {}

    async def _supervisor(state):
        seen["memories"] = state.get("_memories", "")
        return {"tasks": PLAN, "plan_steps": PLAN}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    state = orchestrator.make_initial_state(
        user_request="帮我写一份销量报告", chat_model_id="m"
    )
    state["_skip_synthesizer"] = True

    with track_run(state["run_id"], request="帮我写一份销量报告") as trace:
        asyncio.run(orchestrator.run_agent_loop(state))

    assert seen["memories"], "memory block must reach the supervisor prompt"
    assert "报告一律用中文" in seen["memories"]

    memory_spans = trace.chain.find("memory")
    assert memory_spans
    assert memory_spans[0].status == "ok"           # not a swallowed error
    assert memory_spans[0].attributes["recalled"] >= 1


def test_memory_recall_failure_is_visible_in_the_chain(safety_env, fake_skills, monkeypatch):
    """A broken recall must show up as a span, not vanish silently."""
    import backend.memory as memory_mod
    from backend.agents import orchestrator
    from backend.observability import track_run

    class _Broken:
        async def recall_for_prompt(self, _query):
            raise RuntimeError("memory backend down")

    monkeypatch.setattr(memory_mod, "get_memory_store", lambda: _Broken())
    monkeypatch.setenv("AGENT_MEMORY", "1")

    async def _supervisor(state):
        return {"tasks": PLAN, "plan_steps": PLAN}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    state = orchestrator.make_initial_state(user_request="x", chat_model_id="m")
    state["_skip_synthesizer"] = True

    with track_run(state["run_id"]) as trace:
        asyncio.run(orchestrator.run_agent_loop(state))

    span = trace.chain.find("memory")[0]
    assert span.status == "skipped"
    assert "memory backend down" in span.error


def test_chain_survives_a_run_without_a_trace(safety_env, fake_skills, monkeypatch):
    """No active trace → the disabled chain keeps the loop running."""
    from backend.agents import orchestrator

    async def _supervisor(state):
        return {"tasks": PLAN, "plan_steps": PLAN}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    state = orchestrator.make_initial_state(user_request="x", chat_model_id="m")
    state["_skip_synthesizer"] = True
    result = asyncio.run(orchestrator.run_agent_loop(state))     # no track_run
    assert result["research_results"]


# ── API ────────────────────────────────────────────────────────────────


def test_chain_api_returns_json_and_text(safety_env, fake_skills, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import app
    from backend.observability import save_run, store

    monkeypatch.setattr(store, "runs_dir", lambda: tmp_path / "runs")
    trace = _run_loop(PLAN, "搜索最新数据并画柱状图", monkeypatch)
    save_run(trace.to_dict())

    client = TestClient(app)
    run_id = trace.run_id

    payload = client.get(f"/api/runs/{run_id}/chain").json()
    assert payload["summary"]["spans"] >= 5
    assert any(s["kind"] == "task" for s in payload["spans"])

    text = client.get(f"/api/runs/{run_id}/chain", params={"format": "text"}).json()
    assert "run:整个运行" in text["text"]
    assert "task:research" in text["text"]

    assert client.get("/api/runs/nope/chain").status_code == 404
