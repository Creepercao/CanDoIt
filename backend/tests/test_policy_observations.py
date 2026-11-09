from copy import deepcopy
import pytest
from backend.agents.policy import LinUCB
from backend.agents.policy_observer import observe_plan
from backend.tools.policy_observations import observation_tasks
from backend.tools.policy_observations import summarize
from backend.tools.policy_observations import training_rows


def test_report_reads_run_archive_directory(tmp_path, monkeypatch, capsys):
    import json
    from backend.tools.policy_observations import main

    (tmp_path / "r.json").write_text(json.dumps(record()), encoding="utf-8")
    (tmp_path / "partial.json.tmp").write_text("partial", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["observations", str(tmp_path)])
    main()
    assert json.loads(capsys.readouterr().out)["runs"] == 1

    (tmp_path / "broken.json").write_text("partial", encoding="utf-8")
    with pytest.raises(SystemExit):
        main()
    assert "broken.json" in capsys.readouterr().err


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


def test_template_only_includes_observed_tasks_with_matching_terminal_outcomes():
    from backend.tools.policy_observations import score_template

    saved = finished_record()
    saved["tasks"].append({**saved["tasks"][0], "task_id": "remedial"})
    observed_task = saved["policy_observation"]["tasks"][0]
    saved["policy_observation"]["tasks"].append({**observed_task, "task_id": "never_executed"})
    assert score_template(saved) == {"t": None}
    saved["tasks"][0]["agent"] = "a"
    assert score_template(saved) == {}


@pytest.mark.parametrize("scores", [{}, {"missing": 1}, {"t": None}, {"t": True}])
def test_training_requires_explicit_valid_score(scores):
    with pytest.raises(ValueError):
        training_rows(finished_record(), scores)


def test_summary_keeps_missing_observations_out_of_agreement():
    result = summarize([record(), {"run_id": "disabled"},
                        {"run_id": "skipped", "policy_observation": {"status": "skipped"}}])
    assert result["unavailable_runs"] == 2
    assert result["missing_runs"] == result["skipped_runs"] == 1
    bucket = next(iter(result["models"].values()))
    assert bucket["agreement"] == 0
    assert bucket["disagreements"] == [{"logged_agent": "b", "suggested_agent": "a", "tasks": 1}]
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


def test_batch_orders_scored_runs_by_instant_and_skips_unscored_runs():
    from backend.tools.policy_observations import batch_training_rows

    first = dict(finished_record(), run_id="z", started_at="2026-11-07T23:30:00-01:00")
    second = dict(finished_record(), run_id="a", started_at="2026-11-08T02:00:00+01:00")
    history = [second, {"run_id": "unscored"}, first]
    rows = batch_training_rows(history, {"a": {"t": .9}, "z": {"t": .4}})
    assert [(row["run_id"], row["score"]) for row in rows] == [("z", .4), ("a", .9)]
    with pytest.raises(ValueError, match="present in the history"):
        batch_training_rows(history, {"unknown": {"t": 1}})


@pytest.mark.parametrize("source", ['{"t": 1, "t": 0}', '{"r": {"t": 1}, "r": {"t": 0}}'])
def test_duplicate_score_keys_cannot_silently_overwrite_a_label(tmp_path, source):
    from backend.tools.policy_observations import read_scores

    labels = tmp_path / "scores.json"
    labels.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate score key"):
        read_scores(labels)


def test_archived_runs_can_be_labeled_and_exported_as_valid_samples(tmp_path, monkeypatch):
    import json
    from backend.tools.policy_observations import main
    from backend.tools.policy_samples import samples

    history = tmp_path / "history"
    history.mkdir()
    first = dict(finished_record(), run_id="z", started_at="2026-11-07T22:00:00+00:00")
    second = dict(finished_record(), run_id="a", started_at="2026-11-07T23:00:00+00:00")
    for run in (first, second):
        (history / f"{run['run_id']}.json").write_text(json.dumps(run), encoding="utf-8")

    template = tmp_path / "template.json"
    monkeypatch.setattr("sys.argv", ["observations", str(history / "z.json"),
                                    "--score-template", "--output", str(template)])
    main()
    assert json.loads(template.read_text(encoding="utf-8")) == {"t": None}

    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"a": {"t": .8}, "z": {"t": .6}}), encoding="utf-8")
    samples_path = tmp_path / "samples.json"
    monkeypatch.setattr("sys.argv", ["observations", str(history), "--scores", str(scores),
                                    "--output", str(samples_path)])
    main()
    rows = json.loads(samples_path.read_text(encoding="utf-8"))
    assert [(row["run_id"], row["task_id"]) for row in rows] == [("z", "t"), ("a", "t")]

    assert [agent for agent, *_ in samples(rows)] == ["b", "b"]


@pytest.mark.parametrize("field,value", [("version", 99), ("stage", "remediate"),
    ("candidate_scope", "unknown"), ("model_sha256", "bad")])
def test_unknown_observation_contract_rejected(field, value):
    saved = record()
    saved["policy_observation"][field] = value
    with pytest.raises(ValueError):
        observation_tasks(saved)
