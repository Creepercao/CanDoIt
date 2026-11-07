import asyncio


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
