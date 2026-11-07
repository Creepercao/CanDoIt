from copy import deepcopy
from backend.agents.policy import LinUCB
from backend.agents.policy_store import snapshot
from backend.agents.policy_observer import observe_plan
from backend.agents.policy_observer import observe_runtime
import asyncio


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
