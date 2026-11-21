import json
import subprocess
import sys

import pytest

from backend.chunking import prepare_document
from backend.tools.hierarchy_labels import main
from backend.tests.test_hierarchy_labels import corpus


def test_cli_draft_and_review_reopen_original_snapshots(tmp_path):
    store, snapshot, sid = corpus(tmp_path)
    questions, labels = tmp_path / "questions.json", tmp_path / "labels.json"
    questions.write_text(json.dumps([{"query": "refund"}, {"query": "unknown"}]), encoding="utf-8")
    command = [sys.executable, "-m", "backend.tools.hierarchy_labels", "--store", str(store.path)]
    drafted = subprocess.run(command + ["draft", str(questions), str(labels)], capture_output=True, text=True)
    assert drafted.returncode == 0, drafted.stderr
    assert json.loads(drafted.stdout)["queries"] == 2
    current = store.save(prepare_document("changed", "manual"))
    store.activate(current)
    progress = subprocess.run(command + ["check", str(labels), "--allow-pending"], capture_output=True, text=True)
    assert progress.returncode == 0, progress.stderr
    assert json.loads(progress.stdout)["pending_queries"] == ["refund", "unknown"]
    assert json.loads(progress.stdout)["snapshot_ids"] == [sid]
    unfinished = subprocess.run(command + ["check", str(labels)], capture_output=True, text=True)
    assert unfinished.returncode == 2
    package = json.loads(labels.read_text(encoding="utf-8"))
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    package["rows"][1].update(reviewed=True, answerable=False)
    labels.write_text(json.dumps(package), encoding="utf-8")
    output = tmp_path / "checked.json"
    checked = subprocess.run(command + ["check", str(labels), "--output", str(output)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["complete"]
    assert [row["id"] for row in store.list_versions() if row["active"]] == [current]


def test_cli_protects_output_before_accessing_the_store(tmp_path, monkeypatch, capsys):
    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setattr(sys, "argv", ["labels", "--store", str(missing), "draft", "unused", str(existing)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "already exists" in capsys.readouterr().err
    assert existing.read_text(encoding="utf-8") == "keep"
    assert not missing.exists()


def test_cli_rejects_duplicate_json_keys_without_creating_output(tmp_path, monkeypatch):
    store, _, _ = corpus(tmp_path)
    questions, output = tmp_path / "questions.json", tmp_path / "labels.json"
    questions.write_text('[{"query":"a","query":"b"}]', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["labels", "--store", str(store.path), "draft", str(questions), str(output)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not output.exists()
