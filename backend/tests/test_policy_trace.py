import pytest
from backend.tools.policy_trace import convert
from backend.tools.policy_trace import annotation_template


def test_template_requires_explicit_labels():
    assert annotation_template({"tasks": [{"task_id": "t"}]}) == {
        "t": {"candidates": [], "score": None}}


def test_template_cli(tmp_path, monkeypatch):
    import json
    from backend.tools.policy_trace import main
    record, output = tmp_path / "run.json", tmp_path / "labels.json"
    record.write_text(json.dumps({"tasks": [{"task_id": "t"}]}))
    monkeypatch.setattr("sys.argv", ["policy_trace", str(record), str(output)])
    main()
    assert json.loads(output.read_text())["t"]["score"] is None
    with pytest.raises(SystemExit):
        main()


def test_join_preserves_recorded_costs_and_provenance():
    record = {"run_id": "r", "tasks": [{"task_id": "t", "agent": "a", "attempts": 2, "duration": 9}]}
    rows = convert(record, {"t": {"candidates": ["a", "b"], "score": .8, "duration": 0}})
    assert rows[0]["duration"] == 9
    assert rows[0]["run_id"] == "r"
    with pytest.raises(ValueError):
        convert(record, {"missing": {}})
