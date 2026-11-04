from backend.agents.policy import LinUCB
from backend.agents.policy_store import snapshot


def test_snapshot_is_detached():
    model = LinUCB()
    model.update("a", [1, 0, 0, 1], 1)
    saved = snapshot(model)
    model.update("a", [1, 0, 0, 1], 1)
    assert saved != snapshot(model)
