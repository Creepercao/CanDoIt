import pytest
from backend.tools.policy_trace import convert


def test_join_preserves_recorded_costs_and_provenance():
    record = {"run_id": "r", "tasks": [{"task_id": "t", "agent": "a", "attempts": 2, "duration": 9}]}
    rows = convert(record, {"t": {"candidates": ["a", "b"], "score": .8, "duration": 0}})
    assert rows[0]["duration"] == 9
    assert rows[0]["run_id"] == "r"
    with pytest.raises(ValueError):
        convert(record, {"missing": {}})
