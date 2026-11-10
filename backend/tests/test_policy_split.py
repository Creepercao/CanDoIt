import pytest

from backend.tools.policy_split import split_by_run_time


CUTOFF = "2026-11-10T08:00:00+08:00"


def row(run_id, task_id, started_at, ended_at):
    return {"run_id": run_id, "task_id": task_id, "run_started_at": started_at, "run_ended_at": ended_at,
            "agent": "research", "candidates": ["research"], "score": 1, "attempts": 1, "duration": 1}


def test_temporal_split_keeps_runs_together_and_excludes_crossing_boundary():
    late = row("late", "1", "2026-11-10T00:00:00+00:00", "2026-11-10T00:01:00+00:00")
    early = row("early", "1", "2026-11-09T23:55:00+00:00", "2026-11-09T23:59:00+00:00")
    next_task = dict(early, task_id="2")
    crossing = row("crossing", "1", "2026-11-09T23:55:00+00:00", "2026-11-10T00:00:00+00:00")
    result = split_by_run_time([late, early, crossing, next_task], CUTOFF)
    assert result == {"training": [early, next_task], "evaluation": [late], "excluded_run_ids": ["crossing"]}


def test_temporal_split_rejects_unverifiable_run_provenance():
    original = row("run", "1", "2026-11-09T23:55:00+00:00", "2026-11-09T23:59:00+00:00")
    with pytest.raises(ValueError, match="timezone"):
        split_by_run_time([dict(original, run_started_at="2026-11-09T23:55:00")], CUTOFF)
    inconsistent = dict(original, task_id="2", run_ended_at="2026-11-09T23:58:00+00:00")
    with pytest.raises(ValueError, match="inconsistent timestamps"):
        split_by_run_time([original, inconsistent], CUTOFF)
    with pytest.raises(ValueError, match="end time is before start time"):
        split_by_run_time([dict(original, run_ended_at="2026-11-09T23:50:00+00:00")], CUTOFF)


def test_split_cli_writes_evaluation_array_without_replacing_a_file(tmp_path, monkeypatch, capsys):
    import json
    from backend.tools.policy_split import main

    early = row("early", "1", "2026-11-09T23:55:00+00:00", "2026-11-09T23:59:00+00:00")
    late = row("late", "1", "2026-11-10T00:00:00+00:00", "2026-11-10T00:01:00+00:00")
    source, target = tmp_path / "samples.json", tmp_path / "evaluation.json"
    source.write_text(json.dumps([late, early]), encoding="utf-8")
    args = ["policy_split", str(source), "--cutoff", CUTOFF]
    monkeypatch.setattr("sys.argv", args)
    main()
    assert json.loads(capsys.readouterr().out)["evaluation_rows"] == 1
    monkeypatch.setattr("sys.argv", args + ["--partition", "evaluation", "--output", str(target)])
    main()
    assert json.loads(target.read_text(encoding="utf-8")) == [late]
    with pytest.raises(SystemExit):
        main()
    assert json.loads(target.read_text(encoding="utf-8")) == [late]
