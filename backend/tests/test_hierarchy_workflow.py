"""File-level annotation -> pinned retrieval -> offline comparison, with no model."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_artifact import make_artifact
from backend.tools.hierarchy_export import export_report


def cli(module, *args):
    return subprocess.run([sys.executable, "-m", "backend.tools." + module, *map(str, args)],
                          cwd=Path(__file__).resolve().parents[2],
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                          capture_output=True, text=True, encoding="utf-8", timeout=20)


def test_annotation_to_comparison_and_regression_review_survives_database_removal(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# Refund\n" + "售后条件" * 30, "manual", 20)
    sid = store.save(snapshot)
    store.activate(sid)
    questions, labels = tmp_path / "questions.json", tmp_path / "labels.json"
    questions.write_text(json.dumps([{"query": "refund", "tags": ["售后"]}, {"query": "missing"}]), encoding="utf-8")
    result = cli("hierarchy_labels", "--store", store.path, "draft", questions, labels)
    assert result.returncode == 0, result.stderr
    package = json.loads(labels.read_text(encoding="utf-8"))
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][-1]["id"]])
    package["rows"][1].update(reviewed=True, answerable=False)
    labels.write_text(json.dumps(package), encoding="utf-8")
    store.activate(store.save(prepare_document("new version", "manual")))
    files = {}
    for route in ("body", "rrf"):
        files[route] = tmp_path / (route + ".json")
        result = cli("hierarchy_export", labels, files[route], "--store", store.path, "--route", route, "--bundle")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["snapshot_ids"] == [sid]
    store.path.unlink()  # All subsequent tools use only the saved artifacts.
    output = tmp_path / "comparison.json"
    result = cli("retrieval_eval", files["rrf"], "--baseline", files["body"], "--k", 20, "--output", output)
    assert result.returncode == 0, result.stderr
    comparison = json.loads(output.read_text(encoding="utf-8"))
    assert comparison["delta"]["recall@20"] == 1
    assert comparison["experiments"]["candidate"]["snapshot_ids"] == [sid]
    result = cli("retrieval_eval", files["rrf"], "--baseline", files["body"], "--format", "markdown")
    assert result.returncode == 0, result.stderr
    assert "## 离线实验条件" in result.stdout and sid in result.stdout
    review = tmp_path / "review.json"
    result = cli("retrieval_review", files["body"], "--baseline", files["rrf"], "--k", 20, "--output", review)
    assert result.returncode == 0, result.stderr
    cases = json.loads(review.read_text(encoding="utf-8"))
    assert cases["selected_queries"] == 1
    assert cases["cases"][0]["query"] == "refund"
    assert cases["experiments"]["candidate"]["configuration"]["route"] == "body"
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps([{"metric": "recall@20", "min_delta": 0}]), encoding="utf-8")
    gated = tmp_path / "gated.json"
    result = cli("retrieval_eval", files["body"], "--baseline", files["rrf"], "--k", 20,
                 "--gates", gates, "--output", gated)
    assert result.returncode == 1, result.stderr
    report = json.loads(gated.read_text(encoding="utf-8"))
    assert not report["gate_result"]["passed"]
    assert report["experiments"]["candidate"]["snapshot_ids"] == [sid]


def saved_experiment(tmp_path, text="known", depth=20):
    store = HierarchyStore(tmp_path / "store.sqlite")
    store.activate(store.save(prepare_document(text, "manual")))
    report = export_report([{"query": "missing", "answerable": False, "relevant_ids": []}], store, depth=depth)
    rows = report.pop("rows")
    return make_artifact(rows, report)


@pytest.mark.parametrize("tool", ["retrieval_eval", "retrieval_review"])
@pytest.mark.parametrize("problem", ["different-corpus", "legacy-mix", "tampered", "shallow"])
def test_invalid_experiment_comparisons_never_write_reports(tmp_path, tool, problem):
    artifact = saved_experiment(tmp_path, depth=1 if problem == "shallow" else 20)
    baseline = artifact
    if problem == "different-corpus":
        baseline = saved_experiment(tmp_path, text="new knowledge")
    elif problem == "legacy-mix":
        baseline = artifact["rows"]
    elif problem == "tampered":
        artifact["rows"][0]["ranked_ids"] = ["fake"]
    candidate_path, baseline_path, output = tmp_path / "candidate.json", tmp_path / "baseline.json", tmp_path / "report.json"
    candidate_path.write_text(json.dumps(artifact), encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    result = cli(tool, candidate_path, "--baseline", baseline_path, "--output", output)
    assert result.returncode == 2, result.stdout
    assert not output.exists()
