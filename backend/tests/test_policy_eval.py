from backend.tools.policy_eval import evaluate
from backend.agents.policy import LinUCB
from backend.agents.policy_store import snapshot


def test_frozen_evaluation_does_not_learn_rewards():
    model = LinUCB(alpha=0)
    row = dict(agent="b", candidates=["a", "b"], score=1, attempts=1, duration=0)
    result = evaluate(model, [row, row])
    assert result["agreement"] == 0
    state = snapshot(model)
    evaluate(model, [row])
    assert snapshot(model) == state
