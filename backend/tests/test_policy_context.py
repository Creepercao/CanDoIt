import pytest
from backend.agents.policy_context import plan_contexts


def test_contexts_use_longest_dependency_path_and_direct_count():
    result = plan_contexts({"c": {"a", "b"}, "b": {"a"}, "a": set()}, .5)
    assert result["c"]["depth"] == 2
    assert result["c"]["upstream"] == 2
    assert result["a"]["features"] == [1, 0, 0, .5]


@pytest.mark.parametrize("deps", [{"a": {"x"}}, {"a": {"b"}, "b": {"a"}}])
def test_invalid_graph_rejected(deps):
    with pytest.raises(ValueError):
        plan_contexts(deps, 1)
