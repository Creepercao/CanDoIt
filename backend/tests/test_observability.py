"""Tests for run tracing, the usage callback and run-record storage."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.observability import store, trace as trace_mod
from backend.observability.trace import (
    ModelCall,
    RunTrace,
    TaskRecord,
    build_usage_callback,
    current_agent,
    current_trace,
    estimate_cost,
    set_current_agent,
    track_run,
)


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """Point run storage at a temp directory."""
    monkeypatch.setattr(store, "runs_dir", lambda: tmp_path / "runs")
    return tmp_path / "runs"


# ── trace aggregation ──────────────────────────────────────────────────


def test_trace_aggregates_usage_per_agent():
    trace = RunTrace(run_id="run_1")
    trace.add_call(ModelCall(model="m", agent="research", input_tokens=100,
                             output_tokens=20, total_tokens=120))
    trace.add_call(ModelCall(model="m", agent="research", input_tokens=50,
                             output_tokens=10, total_tokens=60))
    trace.add_call(ModelCall(model="m2", agent="chart", input_tokens=10,
                             output_tokens=5, total_tokens=15))
    trace.add_call(ModelCall(model="m", agent="", input_tokens=1,
                             output_tokens=1, total_tokens=2))

    usage = trace.usage()
    assert usage["calls"] == 4
    assert usage["input_tokens"] == 161
    assert usage["output_tokens"] == 36
    assert usage["total_tokens"] == 197
    assert usage["by_agent"]["research"]["total_tokens"] == 180
    assert usage["by_agent"]["research"]["calls"] == 2
    assert usage["by_agent"]["chart"]["total_tokens"] == 15
    assert usage["by_agent"]["(未归属)"]["calls"] == 1
    assert usage["models"] == {"m": 3, "m2": 1}


def test_trace_task_stats_report_failures_and_retries():
    trace = RunTrace(run_id="run_1")
    trace.add_task(TaskRecord(agent="research", duration=12.0, attempts=1))
    trace.add_task(TaskRecord(agent="chart", duration=3.5, attempts=3, status="failed"))
    trace.add_task(TaskRecord(agent="code", duration=0.5, attempts=1))

    stats = trace.task_stats()
    assert stats["tasks"] == 3
    assert stats["failed"] == 1
    assert stats["retried"] == 1
    assert stats["slowest"] == {"agent": "research", "duration": 12.0}


def test_trace_serialises_to_a_json_safe_record():
    with track_run("run_1", session_id="s1", request="做点什么") as trace:
        trace.add_call(ModelCall(model="m", agent="research", total_tokens=10))
        trace.add_task(TaskRecord(agent="research"))
        trace.note(verification={"status": "complete"})

    record = trace.to_dict()
    assert record["run_id"] == "run_1"
    assert record["session_id"] == "s1"
    assert record["verification"]["status"] == "complete"
    assert record["duration_seconds"] >= 0
    assert isinstance(record["tasks"], list) and len(record["tasks"]) == 1
    json.dumps(record)                     # must be serialisable


def test_track_run_scope_is_nested_and_restored():
    assert current_trace() is None
    with track_run("outer") as outer:
        assert current_trace() is outer
        with track_run("inner") as inner:
            assert current_trace() is inner
        assert current_trace() is outer
    assert current_trace() is None


def test_current_agent_tagging():
    assert current_agent() == ""
    set_current_agent("research")
    assert current_agent() == "research"


def test_agent_tag_is_isolated_per_asyncio_task():
    """Wave tasks run concurrently; the tag must not leak between them."""
    seen: dict[str, str] = {}

    async def worker(name: str):
        set_current_agent(name)
        await asyncio.sleep(0)
        seen[name] = current_agent()

    async def scenario():
        await asyncio.gather(worker("research"), worker("chart"), worker("code"))

    asyncio.run(scenario())
    assert seen == {"research": "research", "chart": "chart", "code": "code"}


# ── pricing ────────────────────────────────────────────────────────────


def test_cost_is_reported_only_when_pricing_is_configured(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_PRICING", raising=False)
    assert estimate_cost(1_000_000, 1_000_000) is None
    assert trace_mod.load_pricing() == {}


def test_cost_estimation_uses_configured_prices(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_PRICING", json.dumps({
        "deepseek-ai/DeepSeek-V3": {"input": 0.27, "output": 1.10},
    }))
    # 2M input + 1M output => 2*0.27 + 1*1.10 = 1.64
    assert estimate_cost(2_000_000, 1_000_000) == pytest.approx(1.64)


def test_cost_matching_prefers_the_longest_model_key(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_PRICING", json.dumps({
        "deepseek": {"input": 1.0, "output": 1.0},
        "deepseek-ai/DeepSeek-V3": {"input": 10.0, "output": 10.0},
    }))
    assert trace_mod.estimate_cost(1_000_000, 0, "deepseek-ai/DeepSeek-V3-0324") == pytest.approx(10.0)


def test_broken_pricing_json_is_ignored(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_PRICING", "{not json")
    assert trace_mod.load_pricing() == {}
    assert estimate_cost(1000, 1000) is None


# ── usage callback ─────────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, input_tokens, output_tokens):
        self.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        self.response_metadata = {"model_name": "fake-model"}


class _FakeGeneration:
    def __init__(self, message):
        self.message = message


class _FakeResult:
    def __init__(self, *messages, llm_output=None):
        self.generations = [[_FakeGeneration(m)] for m in messages]
        self.llm_output = llm_output or {}


def test_callback_records_usage_from_usage_metadata():
    callback = build_usage_callback()
    assert callback is not None

    async def scenario():
        with track_run("run_1") as trace:
            set_current_agent("research")
            await callback.on_llm_start({}, ["hi"], run_id="call-1")
            await callback.on_llm_end(_FakeResult(_FakeMessage(120, 30)), run_id="call-1")
            return trace

    trace = asyncio.run(scenario())
    assert len(trace.calls) == 1
    call = trace.calls[0]
    assert call.agent == "research"
    assert call.input_tokens == 120
    assert call.output_tokens == 30
    assert call.total_tokens == 150
    assert call.model == "fake-model"
    assert call.duration >= 0


def test_callback_accepts_llm_output_style_usage():
    callback = build_usage_callback()

    async def scenario():
        with track_run("run_1") as trace:
            await callback.on_llm_end(_FakeResult(
                llm_output={"model_name": "legacy-model",
                            "token_usage": {"prompt_tokens": 7, "completion_tokens": 3}},
            ))
            return trace

    trace = asyncio.run(scenario())
    call = trace.calls[0]
    assert call.model == "legacy-model"
    assert call.input_tokens == 7
    assert call.output_tokens == 3
    assert call.total_tokens == 10


def test_callback_is_a_noop_without_an_active_trace():
    callback = build_usage_callback()

    async def scenario():
        await callback.on_llm_end(_FakeResult(_FakeMessage(1, 1)))
        return current_trace()

    assert asyncio.run(scenario()) is None      # no crash, nothing recorded


def test_callback_survives_a_response_without_usage():
    callback = build_usage_callback()

    async def scenario():
        with track_run("run_1") as trace:
            await callback.on_llm_end(_FakeResult(_FakeMessage(0, 0)))
            await callback.on_llm_error(RuntimeError("boom"), run_id="x")
            return trace

    trace = asyncio.run(scenario())
    assert len(trace.calls) == 1
    assert trace.calls[0].total_tokens == 0


# ── run record storage ─────────────────────────────────────────────────


def _record(run_id: str, **overrides) -> dict:
    base = {
        "run_id": run_id,
        "session_id": "s1",
        "request": "搜索并画图",
        "started_at": "2026-01-01T00:00:00+00:00",
        "duration_seconds": 12.5,
        "usage": {"calls": 5, "total_tokens": 1200, "cost_usd": None},
        "task_stats": {"tasks": 3, "failed": 1, "retried": 1},
        "verification": {"status": "partial", "summary": "缺图表"},
    }
    base.update(overrides)
    return base


def test_save_and_read_a_run_record(runs_dir):
    path = store.save_run(_record("run_a"))
    assert path is not None and path.exists()

    loaded = store.get_run("run_a")
    assert loaded["request"] == "搜索并画图"
    assert store.get_run("run_missing") is None


def test_list_runs_returns_summaries_newest_first(runs_dir):
    store.save_run(_record("run_a"))
    store.save_run(_record("run_b", request="第二个"))

    listed = store.list_runs()
    assert {item["run_id"] for item in listed} == {"run_a", "run_b"}
    summary = next(item for item in listed if item["run_id"] == "run_a")
    assert summary["status"] == "partial"
    assert summary["total_tokens"] == 1200
    assert summary["model_calls"] == 5          # count only, not the call list
    assert summary["failed_tasks"] == 1
    assert summary["retried_tasks"] == 1
    # Summaries stay small: no per-call detail, no full task list.
    assert "usage" not in summary
    assert "tasks" not in summary or not isinstance(summary["tasks"], list)


def test_list_runs_can_filter_by_session(runs_dir):
    store.save_run(_record("run_a", session_id="s1"))
    store.save_run(_record("run_b", session_id="s2"))
    assert [r["run_id"] for r in store.list_runs(session_id="s2")] == ["run_b"]


def test_unsafe_run_ids_are_rejected(runs_dir):
    for bad in ("../escape", "a/b", "", "x" * 100, "run\u0000x"):
        assert store.save_run(_record(bad)) is None
        assert store.get_run(bad) is None
        assert store.delete_run(bad) is False
    assert not runs_dir.exists() or not list(runs_dir.glob("*"))


def test_delete_run(runs_dir):
    store.save_run(_record("run_a"))
    assert store.delete_run("run_a") is True
    assert store.get_run("run_a") is None
    assert store.delete_run("run_a") is False


def test_saving_is_atomic_and_leaves_no_temp_files(runs_dir):
    store.save_run(_record("run_a"))
    leftovers = [p.name for p in runs_dir.glob("*.tmp")]
    assert leftovers == []


def test_corrupt_records_are_skipped_when_listing(runs_dir):
    store.save_run(_record("run_a"))
    (runs_dir / "broken.json").write_text("{not json", encoding="utf-8")

    listed = store.list_runs()
    assert [item["run_id"] for item in listed] == ["run_a"]
    assert store.get_run("broken") is None


def test_overview_aggregates_the_recent_runs(runs_dir):
    store.save_run(_record("run_a"))
    store.save_run(_record("run_b", usage={"calls": 3, "total_tokens": 800, "cost_usd": 0.5},
                          task_stats={"tasks": 2, "failed": 0, "retried": 0},
                          duration_seconds=7.5))

    summary = store.overview()
    assert summary["runs"] == 2
    assert summary["model_calls"] == 8
    assert summary["total_tokens"] == 2000
    assert summary["failed_tasks"] == 1
    assert summary["retried_tasks"] == 1
    assert summary["avg_duration"] == pytest.approx(10.0)
    assert summary["total_cost_usd"] == pytest.approx(0.5)
    assert summary["status_counts"] == {"partial": 2}


def test_overview_handles_an_empty_history(runs_dir):
    summary = store.overview()
    assert summary["runs"] == 0
    assert summary["total_tokens"] == 0
    assert summary["total_cost_usd"] is None
    assert summary["status_counts"] == {}


# ── orchestrator integration ───────────────────────────────────────────


def test_orchestrator_populates_the_run_trace(safety_env, fake_skills, monkeypatch):
    """The shared loop must record every scheduled task into the active trace."""
    from backend.agents import orchestrator

    plan = [
        {"step": 1, "agent": "research", "prompt": "检索最新数据"},
        {"step": 2, "agent": "analyst", "prompt": "提取结构化数据", "depends_on": [1]},
        {"step": 3, "agent": "chart", "prompt": "画柱状图", "depends_on": [2]},
    ]

    async def _supervisor(state):
        return {"tasks": plan, "plan_steps": plan}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    state = orchestrator.make_initial_state(
        user_request="搜索最新数据并画一个柱状图", chat_model_id="test-model"
    )
    state["_skip_synthesizer"] = True

    with track_run("run_trace", session_id="s1", request="搜索最新数据并画一个柱状图") as trace:
        asyncio.run(orchestrator.run_agent_loop(state))

    assert [task.agent for task in trace.tasks] == ["research", "analyst", "chart"]
    assert all(task.attempts == 1 for task in trace.tasks)
    assert all(task.duration >= 0 for task in trace.tasks)

    record = trace.to_dict()
    assert record["task_stats"]["tasks"] == 3
    assert record["task_stats"]["failed"] == 0
    assert record["session_id"] == "s1"
    json.dumps(record)


def test_orchestrator_records_a_retry_in_the_trace(safety_env, fake_skills, monkeypatch):
    """Retries and failures must show up in the run record, not vanish."""
    from backend.agents import orchestrator
    from backend.skills.base import Skill
    from backend.skills.registry import skill_registry

    calls: list[int] = []

    async def _flaky(state):
        calls.append(1)
        if len(calls) == 1:
            return {"flaky_results": [{"error": "HTTP 429"}]}
        return {"flaky_results": [{"task": "ok"}]}

    skill_registry.register(Skill(
        name="flaky", display_name="Flaky", description="flaky",
        worker=_flaky, result_key="flaky_results",
    ))
    monkeypatch.setenv("AGENT_RETRY_BASE_SECONDS", "0")

    plan = [{"step": 1, "agent": "flaky", "prompt": "取一个数"}]

    async def _supervisor(state):
        return {"tasks": plan, "plan_steps": plan}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    state = orchestrator.make_initial_state(user_request="取一个数", chat_model_id="m")
    state["_skip_synthesizer"] = True

    with track_run("run_retry") as trace:
        asyncio.run(orchestrator.run_agent_loop(state))

    assert len(trace.tasks) == 1
    assert trace.tasks[0].attempts == 2          # the retry is visible
    assert trace.task_stats()["retried"] == 1
