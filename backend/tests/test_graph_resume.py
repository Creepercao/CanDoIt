"""Cross-process state recovery.

The point of the checkpointer is that a paused run does not live in memory: the
graph state is on disk, so a *different* process can pick the thread up and
finish it.  These tests drive the real graph twice against the same SQLite file
and drop every in-memory cache in between, which is what a restart looks like.
"""
from __future__ import annotations

import asyncio

from langgraph.types import Command


def _supervisor_stub(monkeypatch, plan: list[dict]):
    from backend.agents import orchestrator

    async def _supervisor(state):
        return {"tasks": plan, "plan_steps": []}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)


def _initial_state(run_id: str, request: str) -> dict:
    from backend.agents import orchestrator

    state = orchestrator.make_initial_state(user_request=request, chat_model_id="m")
    state["run_id"] = run_id  # pin the thread id so both phases share it
    state["_skip_synthesizer"] = True
    return state


def test_paused_run_resumes_from_sqlite_after_a_restart(
    tmp_path, monkeypatch, safety_env, fake_skills
):
    from backend.agents import graph_flow
    from backend.agents.checkpoint import checkpointer
    from backend.agents.graph_flow import compile_graph

    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CHECKPOINT_PATH", str(tmp_path / "graph.sqlite"))

    # A content-only agent asked to do something destructive: the policy
    # downgrades it to "needs a human", which parks the graph on an interrupt.
    _supervisor_stub(monkeypatch, [{"step": 1, "agent": "code", "prompt": "rm -rf /"}])

    run_id = "run_restart"
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}

    async def phase_one():
        async with checkpointer() as cp:
            graph = compile_graph(cp)
            async for _ in graph.astream(_initial_state(run_id, "帮我清理服务器"), config):
                pass
            return await graph.aget_state(config)

    paused = asyncio.run(phase_one())

    assert paused.next, "the run must be parked on an approval interrupt"
    assert paused.values.get("tasks"), "the plan must be checkpointed before the pause"
    assert not paused.values.get("_refused"), "this case should ask, not refuse"

    # A restart clears every in-process cache; only the SQLite file survives.
    graph_flow._REVIEWERS.clear()
    graph_flow._RUN_SPANS.clear()

    async def phase_two():
        async with checkpointer() as cp:
            graph = compile_graph(cp)
            snapshot = await graph.aget_state(config)

            # The state came back from disk, not from memory.
            assert snapshot.values.get("tasks") == paused.values.get("tasks")
            assert snapshot.next, "the frozen continuation point must survive"

            for _ in range(8):
                async for _ in graph.astream(
                    Command(resume={"approved": True}), config
                ):
                    pass
                snapshot = await graph.aget_state(config)
                if not snapshot.next:
                    break
            return snapshot

    finished = asyncio.run(phase_two())

    assert not finished.next, "approving every gate must let the run finish"
    assert finished.values.get("_verification"), "the tail of the graph must have run"
    assert finished.values.get("code_results"), "the worker result must be persisted"


def test_rejected_resume_stops_the_run(tmp_path, monkeypatch, safety_env, fake_skills):
    """The resume payload decides the outcome; a rejection ends the run."""
    from backend.agents.checkpoint import checkpointer
    from backend.agents.graph_flow import compile_graph

    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CHECKPOINT_PATH", str(tmp_path / "reject.sqlite"))

    _supervisor_stub(monkeypatch, [{"step": 1, "agent": "code", "prompt": "rm -rf /"}])

    run_id = "run_reject"
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}

    async def scenario():
        async with checkpointer() as cp:
            graph = compile_graph(cp)
            async for _ in graph.astream(_initial_state(run_id, "帮我清理服务器"), config):
                pass
            for _ in range(6):
                async for _ in graph.astream(
                    Command(resume={"approved": False}), config
                ):
                    pass
                snapshot = await graph.aget_state(config)
                if not snapshot.next:
                    return snapshot
            return await graph.aget_state(config)

    finished = asyncio.run(scenario())

    assert not finished.next
    assert finished.values.get("_refused") or finished.values.get("final_response")


def test_memory_checkpoint_keeps_runs_isolated(monkeypatch, safety_env, fake_skills):
    """Distinct thread ids must not see each other, even in memory mode."""
    from backend.agents import graph_flow
    from backend.agents.checkpoint import checkpointer
    from backend.agents.graph_flow import compile_graph

    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "memory")
    _supervisor_stub(monkeypatch, [{"step": 1, "agent": "code", "prompt": "写个脚本"}])

    async def scenario():
        async with checkpointer() as cp:
            graph = compile_graph(cp)
            cfg_a = {"configurable": {"thread_id": "iso_a"}, "recursion_limit": 50}
            cfg_b = {"configurable": {"thread_id": "iso_b"}, "recursion_limit": 50}
            async for _ in graph.astream(_initial_state("iso_a", "写个脚本"), cfg_a):
                pass
            snap_a = await graph.aget_state(cfg_a)
            snap_b = await graph.aget_state(cfg_b)
            return snap_a, snap_b

    snap_a, snap_b = asyncio.run(scenario())
    graph_flow._REVIEWERS.clear()

    assert snap_a.values.get("user_request") == "写个脚本"
    assert snap_b.values == {}, "a thread that never ran must stay empty"
