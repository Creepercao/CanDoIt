"""Exercise real file boundaries from batch traces to restored suggestions."""
import json
from backend.tools.policy_trace import main as export
from backend.tools.policy_train import main as train
from backend.tools.policy_suggest import suggest
from backend.agents.policy_store import load


def test_batch_to_saved_model(tmp_path, monkeypatch, capsys):
    runs, labels, rows, model = [tmp_path / name for name in
                                ("runs.json", "labels.json", "rows.json", "model.json")]
    runs.write_text(json.dumps([{"run_id": "r", "started_at": "2026-01-01T00:00:00+00:00",
        "tasks": [{"task_id": "t", "agent": "b", "attempts": 1, "duration": 0}]}]))
    labels.write_text(json.dumps({"r": {"t": {"candidates": ["a", "b"], "score": 1}}}))
    monkeypatch.setattr("sys.argv", ["export", str(runs), str(rows), "--batch", "--annotations", str(labels)])
    export()
    monkeypatch.setattr("sys.argv", ["train", str(rows), "--alpha", "0", "--save-model", str(model)])
    train()
    assert json.loads(capsys.readouterr().out)["observations"] == 1
    assert suggest(load(model), ["a", "b"])["suggested_agent"] == "b"
