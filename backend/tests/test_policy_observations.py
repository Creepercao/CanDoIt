from copy import deepcopy
import pytest
from backend.agents.policy import LinUCB
from backend.agents.policy_observer import observe_plan
from backend.tools.policy_observations import observation_tasks


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
