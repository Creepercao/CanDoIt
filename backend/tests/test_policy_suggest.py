import pytest
from backend.agents.policy import LinUCB
from backend.tools.policy_suggest import suggest


def test_suggest_uses_context_and_only_allowed_candidates():
    model = LinUCB(alpha=0)
    model.update("b", [1, 0, 0, 1], 1)
    assert suggest(model, ["a", "b"])["suggested_agent"] == "b"
    assert suggest(model, ["a"])["suggested_agent"] == "a"
    assert suggest(model, ["a"])["advisory_only"] is True


@pytest.mark.parametrize("candidates", [[], "abc", [None]])
def test_bad_candidates(candidates):
    with pytest.raises(ValueError):
        suggest(LinUCB(), candidates)
