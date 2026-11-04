from backend.agents.policy import LinUCB
from backend.agents.policy_store import snapshot
from backend.agents.policy_store import restore
import pytest


def test_restore_roundtrip_and_bad_matrix():
    model = LinUCB()
    model.update("a", [1, 0, 0, 1], .8)
    saved = snapshot(model)
    assert restore(saved).score("a", [1, 0, 0, 1]) == model.score("a", [1, 0, 0, 1])
    saved["arms"]["a"][0][0][0] = -1
    with pytest.raises(ValueError):
        restore(saved)


def test_snapshot_is_detached():
    model = LinUCB()
    model.update("a", [1, 0, 0, 1], 1)
    saved = snapshot(model)
    model.update("a", [1, 0, 0, 1], 1)
    assert saved != snapshot(model)
