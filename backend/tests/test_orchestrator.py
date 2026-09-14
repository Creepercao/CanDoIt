"""Tests for the shared agent-loop core.

``run_agent_loop`` and ``run_agent_loop_stream`` are two views over one
orchestration generator; these tests pin that equivalence so a future change
cannot silently make the sync and SSE paths diverge.
"""

from __future__ import annotations

import asyncio

from backend.agents import orchestrator
from backend.safety import verifier


def _supervisor_stub(plan: list[dict], direct: str = ""):
    async def _stub(state):
        if direct:
            return {"tasks": [], "plan_steps": [], "final_response": direct}
        return {"tasks": plan, "plan_steps": plan}
    return _stub


def _run(monkeypatch, plan, request, *, direct="", mode="stream", tasks=None):
    """Run one pipeline through the requested entry point."""
    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor_stub(plan, direct))

    state = orchestrator.make_initial_state(
        user_request=request, chat_model_id="test-model", session_id="s1"
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


def _types(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def _final_state(events: list[dict]) -> dict:
    for event in reversed(events):
        if event["event"] == "_agent_loop_done":
            return event["data"]["state"]
    raise AssertionError("no _agent_loop_done event")


CHAIN_PLAN = [
    {"step": 1, "agent": "research", "prompt": "检索最新数据"},
    {"step": 2, "agent": "analyst", "prompt": "提取结构化数据", "depends_on": [1]},
    {"step": 3, "agent": "chart", "prompt": "画柱状图", "depends_on": [2]},
]
CHAIN_REQUEST = "搜索最新数据并画一个柱状图"


# ── event contract ─────────────────────────────────────────────────────


def test_stream_emits_the_documented_event_order(safety_env, fake_skills, monkeypatch):
    _state, events = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="stream")
    types = _types(events)

    assert types[0] == "phase"
    assert "plan" in types
    assert "safety_review" in types
    assert types.count("agent_start") == 3
    assert types.count("agent_done") == 3
    assert "verification" in types
    assert types[-1] == "_agent_loop_done"
    assert types[-2] == "phase"

    # Ordering: plan → review → agents → verify → synthesize → terminal
    assert types.index("plan") < types.index("safety_review")
    assert types.index("safety_review") < types.index("agent_start")
    assert types.index("agent_start") < types.index("agent_done")
    assert types.index("agent_done") < types.index("verification")
    assert types.index("verification") < types.index("_agent_loop_done")


def test_stream_carries_run_id_on_every_safety_event(safety_env, fake_skills, monkeypatch):
    _state, events = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="stream")
    run_ids = {
        e["data"].get("run_id") for e in events
        if e["event"] in ("safety_review", "verification", "plan")
    }
    assert len(run_ids) == 1
    assert run_ids.pop().startswith("run_")


# ── stream / sync equivalence ──────────────────────────────────────────


def test_stream_and_sync_agree_on_results(safety_env, fake_skills, monkeypatch):
    synced, _ = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="sync")
    _ignored, events = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="stream")
    streamed = _final_state(events)

    for key in ("research_results", "analyst_results", "chart_results"):
        assert len(synced[key]) == len(streamed[key]), key

    assert synced["_verification"]["status"] == streamed["_verification"]["status"]
    # Two independent runs must get their own identity, but the same shape.
    assert synced["_safety"]["run_id"] and streamed["_safety"]["run_id"]
    assert synced["_safety"]["run_id"] != streamed["_safety"]["run_id"]
    assert synced["_safety"]["policy"]["checkpoints"] == streamed["_safety"]["policy"]["checkpoints"]
    assert [t["agent"] for t in synced["tasks"]] == [t["agent"] for t in streamed["tasks"]]


def test_both_modes_complete_the_goal(safety_env, fake_skills, monkeypatch):
    synced, _ = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="sync")
    _ignored, events = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="stream")
    streamed = _final_state(events)

    assert synced["_verification"]["status"] == verifier.COMPLETE
    assert streamed["_verification"]["status"] == verifier.COMPLETE


def test_sync_mode_skips_the_synthesizer_when_flagged(safety_env, fake_skills, monkeypatch):
    synced, _ = _run(monkeypatch, CHAIN_PLAN, CHAIN_REQUEST, mode="sync")
    # No provider is reachable in tests, so reaching the synthesizer would hang
    # or error; an empty response proves the flag was honoured.
    assert synced["final_response"] == ""


# ── early exits ────────────────────────────────────────────────────────


def test_direct_reply_exits_before_planning(safety_env, fake_skills, monkeypatch):
    _state, events = _run(monkeypatch, [], "你好", direct="你好！", mode="stream")
    types = _types(events)

    assert "plan" not in types
    assert "agent_start" not in types
    assert types[:2] == ["phase", "phase"]
    assert types[-1] == "_agent_loop_done"
    final = next(e for e in events if e["event"] == "final")
    assert final["data"]["response"] == "你好！"
    assert _final_state(events)["final_response"] == "你好！"


def test_direct_reply_survives_the_sync_path(safety_env, fake_skills, monkeypatch):
    synced, _ = _run(monkeypatch, [], "你好", direct="你好！", mode="sync")
    assert synced["final_response"] == "你好！"
    assert synced["_safety"]["policy"]["checkpoints"] == 0


def test_empty_plan_after_screening_still_returns_state(safety_env, fake_skills, monkeypatch):
    plan = [{"step": 1, "agent": "ghost", "prompt": "未注册代理"}]
    synced, _ = _run(monkeypatch, plan, "随便干点什么", mode="sync")
    assert synced["dropped_tasks"][0]["agent"] == "ghost"
    assert "未注册" in synced["final_response"] or synced["final_response"]


# ── HTML artifact handling now shared by both paths ────────────────────


def _register_html_skill():
    from backend.skills.base import Skill
    from backend.skills.registry import skill_registry

    async def _worker(state):
        task = state["_current_task"]
        return {"skill_outputs": {"html_skill": [{
            "task": task["prompt"],
            "result": "<!DOCTYPE html><html><body>报告</body></html>",
        }]}}

    skill_registry.register(Skill(
        name="html_skill", display_name="HTML Skill", description="产出 HTML",
        worker=_worker, result_key="skill_outputs",
    ))


def test_html_output_is_saved_on_both_paths(safety_env, fake_skills, monkeypatch):
    import backend.api as api

    saved: list[tuple] = []

    def _fake_save(skill_name, content, title=""):
        saved.append((skill_name, title))
        return {"html_url": f"/outputs/skills/{skill_name}.html", "title": title or skill_name,
                "file_path": "/tmp/fake", "source_skill": skill_name}

    monkeypatch.setattr(api, "save_skill_html", _fake_save)
    _register_html_skill()

    plan = [{"step": 1, "agent": "html_skill", "prompt": "生成报告"}]

    synced, _ = _run(monkeypatch, plan, "生成一份 HTML 报告", mode="sync")
    assert saved, "sync path must persist HTML artifacts too"
    assert synced["skill_outputs"]["html_skill"][0]["html_url"].endswith(".html")

    saved.clear()
    _ignored, events = _run(monkeypatch, plan, "生成一份 HTML 报告", mode="stream")
    streamed = _final_state(events)
    assert saved, "streaming path must persist HTML artifacts"
    assert streamed["skill_outputs"]["html_skill"][0]["html_url"].endswith(".html")

    loop_done = next(e for e in events if e["event"] == "_agent_loop_done")
    assert loop_done["data"]["collected_html"][0]["skill_name"] == "html_skill"
