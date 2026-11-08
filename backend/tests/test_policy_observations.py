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


def test_report_cli_preserves_previous_report(tmp_path, monkeypatch):
    import json
    from backend.tools.policy_observations import main
    source, output = tmp_path / "runs.json", tmp_path / "report.json"
    source.write_text(json.dumps([record()]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["observations", str(source), "--output", str(output)])
    main()
    original = output.read_bytes()
    assert json.loads(original)["runs"] == 1
    with pytest.raises(SystemExit):
        main()
    assert output.read_bytes() == original


def test_sample_export_cli(tmp_path, monkeypatch, capsys):
    import json
    from backend.tools.policy_observations import main
    from backend.tools.policy_train import fit
    source, labels = tmp_path / "run.json", tmp_path / "scores.json"
    source.write_text(json.dumps(finished_record()), encoding="utf-8")
    labels.write_text(json.dumps({"t": .8}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["observations", str(source), "--scores", str(labels)])
    main()
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["run_id"] == "r"
    assert "b" in fit(rows).arms


@pytest.mark.parametrize("field,value", [("version", 99), ("stage", "remediate"),
    ("candidate_scope", "unknown"), ("model_sha256", "bad")])
def test_unknown_observation_contract_rejected(field, value):
    saved = record()
    saved["policy_observation"][field] = value
    with pytest.raises(ValueError):
        observation_tasks(saved)
