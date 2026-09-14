"""Tests for task-level retry and incremental re-planning."""

from __future__ import annotations

import asyncio

import pytest

from backend.agents import orchestrator, recovery
from backend.safety import policy
from backend.safety import verifier


# ── unit level ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("Error code: 429 - rate limit exceeded", recovery.TRANSIENT),
    ("Request timed out after 120s", recovery.TRANSIENT),
    ("Connection reset by peer", recovery.TRANSIENT),
    ("502 Bad Gateway", recovery.TRANSIENT),
    ("Service temporarily unavailable", recovery.TRANSIENT),
    ("ValueError: invalid literal for int()", recovery.SEMANTIC),
    ("", recovery.SEMANTIC),
    ("搜索结果为空", recovery.SEMANTIC),
])
def test_classify_failure(text, expected):
    assert recovery.classify_failure(text) == expected


def test_backoff_grows_and_is_capped(monkeypatch):
    monkeypatch.setenv("AGENT_RETRY_BASE_SECONDS", "2")
    monkeypatch.setenv("AGENT_RETRY_MAX_SECONDS", "5")
    assert recovery.backoff_seconds(1) == 2.0
    assert recovery.backoff_seconds(2) == 4.0
    assert recovery.backoff_seconds(3) == 5.0   # capped

    monkeypatch.setenv("AGENT_RETRY_BASE_SECONDS", "0")
    assert recovery.backoff_seconds(1) == 0.0


def test_is_unusable_detects_errors_and_empties():
    assert recovery.is_unusable({})
    assert recovery.is_unusable({"research_results": []})
    assert recovery.is_unusable({"research_results": [{"error": "boom"}]})
    assert not recovery.is_unusable({"research_results": [{"task": "x"}]})
    # A nested result error also counts.
    assert recovery.is_unusable({"image_results": [{"result": {"error": "no model"}}]})


def test_failure_reason_prefers_the_error_text():
    assert "boom" in recovery.failure_reason({"research_results": [{"error": "boom"}]})
    assert "research_results" in recovery.failure_reason({"research_results": []})


def test_retry_task_carries_feedback_without_losing_the_original():
    original = {"id": "code:1", "agent": "code", "prompt": "写一个函数"}
    retried = recovery.retry_task(original, 1, "产出为空", transient=False)

    assert retried["prompt"].startswith("写一个函数")
    assert "重试反馈" in retried["prompt"]
    assert "产出为空" in retried["prompt"]
    assert retried["id"] == "code:1"
    assert retried["_attempt"] == 2
    assert retried["_retry_of"] == "code:1"
    assert "_retry_of" not in original            # no mutation of the original

    transient = recovery.retry_task(original, 0, "429", transient=True)
    assert "429" in transient["prompt"]
    assert "可恢复的服务问题" in transient["prompt"]


def test_policy_public_predicates_match_the_private_helpers():
    payload = {"a": [{"error": "x"}]}
    assert policy.output_has_error(payload) is True
    assert policy.first_error(payload) == "x"
    assert policy.output_is_empty({"a": []}) is True
    assert policy.output_is_empty({"a": [1]}) is False


# ── retry inside the orchestrator ──────────────────────────────────────


@pytest.fixture
def retry_env(safety_env, monkeypatch):
    """Fast, isolated retries: no backoff sleep and no recovery round.

    Recovery has its own tests; keeping it off here means ``agent_retry``
    counts and worker call counts describe the planned steps only.
    """
    monkeypatch.setenv("AGENT_RETRY_BASE_SECONDS", "0")
    monkeypatch.setenv("AGENT_RETRY_MAX_SECONDS", "0")
    # Flipped on the settings object: it is read when the singleton is built,
    # which happens in the safety_env fixture before this one runs.
    safety_env.auto_remediate = False


def _register_flaky(name: str, outputs: list[dict]) -> list[str]:
    """Register ``name`` so its i-th call returns the i-th full output dict.

    The last output repeats once the list is exhausted.  Returns the prompts
    the worker actually received so tests can assert on the retry feedback.
    """
    from backend.skills.base import Skill
    from backend.skills.registry import skill_registry

    seen: list[str] = []

    async def _worker(state):
        task = state["_current_task"]
        seen.append(task.get("prompt", ""))
        return outputs[min(len(seen) - 1, len(outputs) - 1)]

    skill_registry.register(Skill(
        name=name, display_name=name, description="flaky",
        worker=_worker, result_key=f"{name}_results",
    ))
    return seen


def _worker_calls(seen: list[str]) -> list[str]:
    """Calls made by the planned steps, excluding remediation re-runs."""
    return [p for p in seen if "补齐未完成的产出" not in p]


def _run(monkeypatch, plan, request, *, mode="stream", replan=None):
    async def _supervisor(state):
        return {"tasks": plan, "plan_steps": plan}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)
    if replan is not None:
        monkeypatch.setattr(recovery, "replan", replan)

    state = orchestrator.make_initial_state(
        user_request=request, chat_model_id="test-model"
    )
    state["_skip_synthesizer"] = True

    if mode == "sync":
        return asyncio.run(orchestrator.run_agent_loop(state)), []

    events: list[dict] = []

    async def collect():
        async for event in orchestrator.run_agent_loop_stream(state, {}, {}):
            events.append(event)

    asyncio.run(collect())
    return None, events


def _state_of(events) -> dict:
    for event in reversed(events):
        if event["event"] == "_agent_loop_done":
            return event["data"]["state"]
    raise AssertionError("no terminal state event")


def test_transient_failure_is_retried_and_eventually_succeeds(
    safety_env, fake_skills, retry_env, monkeypatch
):
    seen = _register_flaky("flaky", [
        {"flaky_results": [{"error": "HTTP 429 Too Many Requests"}]},
        {"flaky_results": [{"task": "ok", "value": 42}]},
    ])
    plan = [{"step": 1, "agent": "flaky", "prompt": "取一个数"}]

    _state, events = _run(monkeypatch, plan, "取一个数")

    retries = [e for e in events if e["event"] == "agent_retry"]
    assert len(retries) == 1
    assert retries[0]["data"]["kind"] == recovery.TRANSIENT
    assert retries[0]["data"]["attempt"] == 2
    assert "429" in retries[0]["data"]["reason"]
    assert len(_worker_calls(seen)) == 2           # the worker really ran twice

    done = next(e for e in events if e["event"] == "agent_done")
    assert done["data"]["attempts"] == 2
    assert _state_of(events)["flaky_results"][0]["value"] == 42


def test_semantic_failure_is_retried_with_feedback(
    safety_env, fake_skills, retry_env, monkeypatch
):
    seen = _register_flaky("flaky", [
        {"flaky_results": []},                     # empty → unusable
        {"flaky_results": [{"task": "ok", "value": 7}]},
    ])
    plan = [{"step": 1, "agent": "flaky", "prompt": "取一个数"}]

    _state, events = _run(monkeypatch, plan, "取一个数")

    retries = [e for e in events if e["event"] == "agent_retry"]
    assert retries and retries[0]["data"]["kind"] == recovery.SEMANTIC
    assert retries[0]["data"]["backoff_seconds"] == 0
    calls = _worker_calls(seen)
    assert len(calls) == 2
    # The second attempt must have been told what went wrong.
    assert "重试反馈" in calls[1]


def test_retry_stops_at_the_attempt_cap(
    safety_env, fake_skills, retry_env, monkeypatch
):
    monkeypatch.setenv("AGENT_TASK_MAX_ATTEMPTS", "2")
    seen = _register_flaky("flaky", [{"flaky_results": [{"error": "still broken"}]}])
    plan = [{"step": 1, "agent": "flaky", "prompt": "取一个数"}]

    _state, events = _run(monkeypatch, plan, "取一个数")

    assert len(_worker_calls(seen)) == 2           # exactly max_attempts
    assert len([e for e in events if e["event"] == "agent_retry"]) == 1
    done = next(e for e in events if e["event"] == "agent_done")
    assert done["data"]["attempts"] == 2
    assert _state_of(events)["flaky_results"][0]["error"] == "still broken"


def test_retry_is_capped_per_run(
    safety_env, fake_skills, retry_env, monkeypatch
):
    monkeypatch.setenv("AGENT_TASK_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("AGENT_MAX_RETRIES", "1")
    seen_a = _register_flaky("flaky_a", [{"flaky_a_results": [{"error": "nope a"}]}])
    seen_b = _register_flaky("flaky_b", [{"flaky_b_results": [{"error": "nope b"}]}])
    plan = [
        {"step": 1, "agent": "flaky_a", "prompt": "a"},
        {"step": 2, "agent": "flaky_b", "prompt": "b"},
    ]

    _state, events = _run(monkeypatch, plan, "同时做两件事")

    retries = [e for e in events if e["event"] == "agent_retry"]
    assert len(retries) == 1                       # run-level cap wins
    assert len(_worker_calls(seen_a)) + len(_worker_calls(seen_b)) == 3   # 2 + 1
    assert _state_of(events)["_safety"]["budget"]["retries"] == 1


def test_successful_task_is_never_retried(safety_env, fake_skills, retry_env, monkeypatch):
    seen = _register_flaky("flaky", [{"flaky_results": [{"task": "ok", "value": 1}]}])
    plan = [{"step": 1, "agent": "flaky", "prompt": "取一个数"}]

    _state, events = _run(monkeypatch, plan, "取一个数")

    assert len(_worker_calls(seen)) == 1
    assert not [e for e in events if e["event"] == "agent_retry"]
    assert next(e for e in events if e["event"] == "agent_done")["data"]["attempts"] == 1


# ── incremental re-planning ────────────────────────────────────────────


def _replan_stub(plan: list[dict], reason: str = "换个思路"):
    calls: list[dict] = []

    async def _stub(**kwargs):
        calls.append(kwargs)
        return plan, reason

    return _stub, calls


def test_llm_replan_replaces_the_mechanical_remediation(
    safety_env, fake_skills, retry_env, monkeypatch
):
    monkeypatch.setenv("AGENT_LLM_REPLAN", "1")
    safety_env.auto_remediate = True
    _register_flaky("flaky", [{"flaky_results": [{"error": "always fails"}]}])
    stub, calls = _replan_stub([{
        "agent": "code", "prompt": "换用脚本生成结果", "_replanned": True,
    }])
    plan = [{"step": 1, "agent": "flaky", "prompt": "生成结果"}]

    _state, events = _run(monkeypatch, plan, "帮我生成结果", replan=stub)

    assert calls, "the planner should have been consulted"
    assert calls[0]["user_request"] == "帮我生成结果"
    assert calls[0]["catalog"]                      # catalog was passed through

    remediations = [e for e in events if e["event"] == "remediation"]
    assert len(remediations) == 1
    assert remediations[0]["data"]["strategy"] == "llm_replan"
    assert remediations[0]["data"]["reason"] == "换个思路"
    assert remediations[0]["data"]["tasks"][0]["agent"] == "code"

    state = _state_of(events)
    assert "code" in {t["agent"] for t in state["tasks"]}
    assert state["code_results"], "the re-planned step must actually run"


def test_mechanical_remediation_is_the_fallback(
    safety_env, fake_skills, retry_env, monkeypatch
):
    safety_env.auto_remediate = True
    monkeypatch.setenv("AGENT_LLM_REPLAN", "1")
    _register_flaky("flaky", [{"flaky_results": [{"error": "always fails"}]}])
    stub, _calls = _replan_stub([])                 # planner returns nothing
    plan = [{"step": 1, "agent": "flaky", "prompt": "生成结果"}]

    _state, events = _run(monkeypatch, plan, "帮我生成结果并画图表", replan=stub)

    remediations = [e for e in events if e["event"] == "remediation"]
    assert remediations, "mechanical remediation should still fire"
    assert remediations[0]["data"]["strategy"] == "mechanical"
    agents = {t["agent"] for t in remediations[0]["data"]["tasks"]}
    assert "chart" in agents or "analyst" in agents


def test_no_recovery_when_the_goal_is_already_complete(
    safety_env, fake_skills, retry_env, monkeypatch
):
    monkeypatch.setenv("AGENT_LLM_REPLAN", "1")
    stub, calls = _replan_stub([{"agent": "code", "prompt": "多余的一步"}])
    plan = [{"step": 1, "agent": "research", "prompt": "检索资料"}]

    _state, events = _run(monkeypatch, plan, "查一下资料", replan=stub)

    assert not calls, "a complete run must not trigger a re-plan"
    assert not [e for e in events if e["event"] == "remediation"]
    assert _state_of(events)["_verification"]["status"] == verifier.COMPLETE


def test_replan_is_bounded_by_the_replan_budget(
    safety_env, fake_skills, retry_env, monkeypatch
):
    monkeypatch.setenv("AGENT_LLM_REPLAN", "1")
    safety_env.auto_remediate = True
    monkeypatch.setenv("AGENT_MAX_REPLANS", "1")
    _register_flaky("flaky", [{"flaky_results": [{"error": "always fails"}]}])
    stub, calls = _replan_stub([{
        "agent": "flaky", "prompt": "再试一次但依然会失败",
    }])
    plan = [{"step": 1, "agent": "flaky", "prompt": "生成结果"}]

    _state, events = _run(monkeypatch, plan, "帮我生成结果", replan=stub)

    assert len(calls) == 1, "only one re-plan round is allowed"
    assert len([e for e in events if e["event"] == "remediation"]) == 1


def test_replan_disabled_by_env(safety_env, fake_skills, retry_env, monkeypatch):
    safety_env.auto_remediate = True
    monkeypatch.setenv("AGENT_LLM_REPLAN", "0")
    _register_flaky("flaky", [{"flaky_results": [{"error": "always fails"}]}])
    stub, calls = _replan_stub([{"agent": "code", "prompt": "换思路"}])
    plan = [{"step": 1, "agent": "flaky", "prompt": "生成结果"}]

    _state, events = _run(monkeypatch, plan, "帮我生成结果", replan=stub)

    assert not calls
    remediations = [e for e in events if e["event"] == "remediation"]
    assert remediations and remediations[0]["data"]["strategy"] == "mechanical"


def test_replan_drops_unknown_and_workerless_agents():
    """The planner may hallucinate; only runnable, known agents get scheduled."""
    async def _fake_llm(*_args, **_kwargs):
        class _Resp:
            content = ('{"reason": "x", "plan": ['
                       '{"agent": "ghost", "prompt": "不存在"},'
                       '{"agent": "broken", "prompt": "无 worker"},'
                       '{"agent": "code", "prompt": "存在"}]}')
        return _Resp()

    class _FakeModel:
        async def ainvoke(self, _messages):
            return await _fake_llm()

    import backend.models.provider as provider_mod
    original = provider_mod.create_chat_model
    provider_mod.create_chat_model = lambda *a, **k: _FakeModel()
    try:
        catalog = {
            "code": {"has_worker": True, "description": "代码", "depends_on": []},
            # Registered but without a runnable worker → must also be dropped.
            "broken": {"has_worker": False, "description": "坏", "depends_on": []},
        }
        planned, reason = asyncio.run(recovery.replan(
            user_request="x", tasks=[], state={}, verdict=None, catalog=catalog,
        ))
    finally:
        provider_mod.create_chat_model = original

    assert [t["agent"] for t in planned] == ["code"]
    assert reason == "x"


def test_replan_returns_empty_when_the_model_is_unavailable(monkeypatch):
    import backend.models.provider as provider_mod

    def _boom(*_a, **_k):
        raise RuntimeError("no provider configured")

    original = provider_mod.create_chat_model
    provider_mod.create_chat_model = _boom
    try:
        planned, _reason = asyncio.run(recovery.replan(
            user_request="x", tasks=[], state={}, verdict=None,
            catalog={"code": {"has_worker": True}},
        ))
    finally:
        provider_mod.create_chat_model = original

    assert planned == []
