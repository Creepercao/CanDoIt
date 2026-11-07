from copy import deepcopy
from backend.agents.policy import LinUCB
from backend.agents.policy_store import snapshot
from backend.agents.policy_observer import observe_plan
from backend.agents.policy_observer import observe_runtime
import asyncio


def test_observation_persists_through_existing_run_store(tmp_path, monkeypatch):
    from backend.agents.policy_observer import record_observation
    from backend.observability import track_run
    from backend.observability import store
    monkeypatch.setattr(store, "runs_dir", lambda: tmp_path)
    observation = {"advisory_only": True, "tasks": [{"task_id": "t"}]}
    with track_run("r") as trace:
        record_observation("other", observation)
        assert "policy_observation" not in trace.extra
        record_observation("r", observation)
        record_observation("r", observation)
        observation["tasks"].clear()
        store.save_run(trace.to_dict())
    saved = store.get_run("r")["policy_observation"]
    assert saved["tasks"] == [{"task_id": "t"}]


def test_runtime_is_disabled_by_default_and_missing_model_is_nonfatal(monkeypatch):
    monkeypatch.delenv("AGENT_POLICY_OBSERVE", raising=False)
    assert asyncio.run(observe_runtime(None, None)) == {}
    monkeypatch.setenv("AGENT_POLICY_OBSERVE", "1")
    monkeypatch.delenv("AGENT_POLICY_MODEL", raising=False)
    assert asyncio.run(observe_runtime(None, None))["status"] == "skipped"


def test_observer_preserves_model_tasks_and_records_disagreement():
    model = LinUCB(alpha=0)
    tasks = [{"id": "t", "agent": "b", "prompt": "private content"}]
    before = deepcopy(tasks), snapshot(model)
    result = observe_plan(model, tasks, {"t": set()}, ["b", "a"], 1)
    assert result["tasks"][0]["suggested_agent"] == "a"
    assert "prompt" not in result["tasks"][0]
    assert (tasks, snapshot(model)) == before
    assert result["advisory_only"] is True
