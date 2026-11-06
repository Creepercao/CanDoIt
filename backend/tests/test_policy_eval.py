from backend.tools.policy_eval import evaluate
from backend.agents.policy import LinUCB
from backend.agents.policy_store import snapshot
from backend.tools.policy_eval import validate_split
import pytest


def test_split_rejects_run_leakage():
    row = dict(agent="a", candidates=["a"], score=1, attempts=1, duration=0,
               run_id="r", task_id="t")
    with pytest.raises(ValueError):
        validate_split([row], [{**row, "task_id": "other"}])
    validate_split([row], [{**row, "run_id": "new"}])


def test_frozen_evaluation_does_not_learn_rewards():
    model = LinUCB(alpha=0)
    initial = snapshot(model)
    row = dict(agent="b", candidates=["a", "b"], score=1, attempts=1, duration=0)
    result = evaluate(model, [row, row])
    assert result["agreement"] == 0
    assert snapshot(model) == initial
    repeated = evaluate(model, [row, row])
    assert repeated["model_sha256"] == result["model_sha256"]
    assert repeated["samples_sha256"] == result["samples_sha256"]
    assert evaluate(model, [row])["samples_sha256"] != result["samples_sha256"]
    state = snapshot(model)
    evaluate(model, [row])
    assert snapshot(model) == state
