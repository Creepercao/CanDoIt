from copy import deepcopy
import pytest
from backend.agents.policy import LinUCB
from backend.agents.policy_observer import observe_plan
from backend.tools.policy_observations import observation_tasks
from backend.tools.policy_observations import summarize
from backend.tools.policy_observations import training_rows


def finished_record():
    saved = record()
    saved.update(ended_at="2026-11-08T00:00:00+00:00", tasks=[
        {"task_id": "t", "agent": "b", "attempts": 2, "duration": 12, "status": "done"}])
    return saved


def test_training_joins_observed_features_and_real_costs():
    saved = finished_record()
    rows = training_rows(saved, {"t": .8})
    assert rows[0]["duration"] == 12
    assert rows[0]["budget_fraction"] == .5
    assert rows[0]["candidates"] == ["a", "b"]
    saved["tasks"][0]["agent"] = "a"
    with pytest.raises(ValueError):
        training_rows(saved, {"t": .8})


@pytest.mark.parametrize("scores", [{}, {"missing": 1}, {"t": None}, {"t": True}])
def test_training_requires_explicit_valid_score(scores):
    with pytest.raises(ValueError):
        training_rows(finished_record(), scores)


def test_summary_keeps_missing_observations_out_of_agreement():
    result = summarize([record(), {"run_id": "disabled"},
                        {"run_id": "skipped", "policy_observation": {"status": "skipped"}}])
    assert result["unavailable_runs"] == 2
    assert next(iter(result["models"].values()))["agreement"] == 0
    with pytest.raises(ValueError):
        summarize([record(), record()])


def record():
    return {"run_id": "r", "policy_observation": observe_plan(LinUCB(alpha=0),
            [{"id": "t", "agent": "b"}], {"t": set()}, ["a", "b"], .5)}


def test_validates_persisted_scores_and_context():
    saved = record()
    assert observation_tasks(saved)[0]["agent"] == "b"
    bad = deepcopy(saved)
    bad["policy_observation"]["tasks"][0]["matched"] = True
    with pytest.raises(ValueError):
        observation_tasks(bad)
    saved["policy_observation"]["tasks"][0]["features"] = []
    with pytest.raises(ValueError):
        observation_tasks(saved)
