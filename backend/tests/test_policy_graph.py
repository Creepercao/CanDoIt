import asyncio
import pytest


def test_plan_records_observation_without_reassigning_tasks(monkeypatch, safety_env, fake_skills):
    from backend.agents import graph_flow as gf, orchestrator as orch
    from backend.agents import policy_observer

    async def supervisor(state):
        return {"tasks": [{"id": "t", "agent": "research", "prompt": "research a topic"}]}

    async def observe(tasks, budget):
        return {"advisory_only": True, "tasks": [{"task_id": "t", "suggested_agent": "code"}]}

    monkeypatch.setattr(orch, "supervisor_node", supervisor)
    monkeypatch.setattr(orch, "validate_and_complete_plan", lambda tasks, request: tasks)
    monkeypatch.setattr(policy_observer, "observe_runtime", observe)
    state = {"run_id": "policy_plan", "user_request": "research a topic"}
    try:
        result = asyncio.run(gf.plan_node(state))
        assert result["tasks"][0]["agent"] == "research"
        assert result["_policy_observation"]["tasks"][0]["suggested_agent"] == "code"
        assert "_policy_observation" in gf._state_schema().__annotations__
    finally:
        gf.release_reviewer("policy_plan")


@pytest.mark.parametrize("mode", ["enabled", "disabled", "broken_model"])
def test_real_model_observation_survives_sqlite_checkpoint(
    mode, tmp_path, monkeypatch, safety_env, fake_skills
):
    from backend.agents import graph_flow as gf, orchestrator as orch
    from backend.agents.checkpoint import checkpointer
    from backend.agents.policy import LinUCB
    from backend.agents.policy_store import save
    from backend.observability import track_run, store

    model_path = tmp_path / "model.json"
    if mode == "broken_model":
        model_path.write_text("invalid JSON", encoding="utf-8")
    else:
        model = LinUCB(alpha=0)
        model.update("code", [1, 0, 0, 1], 1)
        save(model, model_path)
    monkeypatch.setenv("AGENT_POLICY_OBSERVE", "0" if mode == "disabled" else "1")
    monkeypatch.setenv("AGENT_POLICY_MODEL", str(model_path))
    monkeypatch.setenv("AGENT_MEMORY", "0")
    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CHECKPOINT_PATH", str(tmp_path / "graph.sqlite"))
    monkeypatch.setattr(store, "runs_dir", lambda: tmp_path / "runs")

    async def supervisor(state):
        return {"tasks": [{"id": "t", "agent": "research", "prompt": "research a topic"}]}

    monkeypatch.setattr(orch, "supervisor_node", supervisor)
    monkeypatch.setattr(orch, "validate_and_complete_plan", lambda tasks, request: tasks)
    run_id = "policy_integration"
    config = {"configurable": {"thread_id": run_id}}

    async def scenario():
        async with checkpointer() as cp:
            graph = gf.build_graph().compile(checkpointer=cp, interrupt_before=["review_plan"])
            with track_run(run_id) as trace:
                await graph.ainvoke({"run_id": run_id, "user_request": "research a topic"}, config)
                store.save_run(trace.to_dict())
        # Reopen the actual database, as another process would.
        async with checkpointer() as cp:
            graph = gf.build_graph().compile(checkpointer=cp)
            return (await graph.aget_state(config)).values

    try:
        state = asyncio.run(scenario())
        assert state["tasks"][0]["agent"] == "research"
        observation = state["_policy_observation"]
        if mode == "disabled":
            assert observation == {}
            assert "policy_observation" not in store.get_run(run_id)
        else:
            assert store.get_run(run_id)["policy_observation"] == observation
            if mode == "enabled":
                assert observation["tasks"][0]["suggested_agent"] == "code"
                assert observation["tasks"][0]["agent"] == "research"
            else:
                assert observation["status"] == "skipped"
    finally:
        gf.release_reviewer(run_id)
