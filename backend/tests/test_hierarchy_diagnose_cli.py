import hashlib
import json

from backend.tests.test_hierarchy_diagnose import corpus
from backend.tests.test_hierarchy_workflow import cli


def test_single_diagnosis_cli_uses_history_and_records_label_file(tmp_path):
    store, snapshot, sid = corpus(tmp_path)
    store.deactivate("manual")
    source = tmp_path / "ids.json"
    source.write_text(json.dumps([snapshot["chunks"][0]["id"]]), encoding="utf-8")
    result = cli("hierarchy_store", "--store", store.path, "diagnose", "needle", source, "--snapshot-id", sid)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["selection"] == "snapshots"
    assert report["snapshot_ids"] == [sid]
    assert report["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["relevance_trace"][0]["title_rank"] is not None
    source.write_text('["foreign"]', encoding="utf-8")
    output = tmp_path / "bad.json"
    result = cli("hierarchy_store", "--store", store.path, "--output", output, "diagnose", "needle", source, "--snapshot-id", sid)
    assert result.returncode == 2 and not output.exists()


def test_batch_cli_diagnoses_pinned_labels_and_protects_saved_report(tmp_path):
    from backend.tools.hierarchy_labels import draft_annotations
    store, snapshot, sid = corpus(tmp_path)
    package = draft_annotations([{"query": "needle", "tags": ["售后"]}], store.prepare())
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    source, output = tmp_path / "labels.json", tmp_path / "diagnosis.json"
    source.write_text(json.dumps(package), encoding="utf-8")
    store.deactivate("manual")
    result = cli("hierarchy_diagnose", source, "--store", store.path, "--output", output)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["snapshot_ids"] == [sid]
    assert report["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["slices"]["售后"]["relevant_chunks"] == 1
    before = output.read_bytes()
    result = cli("hierarchy_diagnose", source, "--store", store.path, "--output", output)
    assert result.returncode == 2 and output.read_bytes() == before
    package["rows"][0]["reviewed"] = False
    source.write_text(json.dumps(package), encoding="utf-8")
    pending = tmp_path / "pending.json"
    result = cli("hierarchy_diagnose", source, "--store", store.path, "--output", pending)
    assert result.returncode == 2 and not pending.exists()


def test_markdown_cli_saves_fingerprint_and_human_readable_stages(tmp_path):
    from backend.tools.hierarchy_labels import draft_annotations
    store, snapshot, sid = corpus(tmp_path)
    package = draft_annotations([{"query": "needle"}], store.prepare())
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    source, output = tmp_path / "labels.json", tmp_path / "diagnosis.md"
    source.write_text(json.dumps(package), encoding="utf-8")
    result = cli("hierarchy_diagnose", source, "--store", store.path, "--output", output, "--format", "markdown", "--context-chars", 1)
    assert result.returncode == 0, result.stderr
    report = output.read_text(encoding="utf-8")
    assert sid in report and hashlib.sha256(source.read_bytes()).hexdigest() in report
    assert "## 逐题证据" in report and "章节没有匹配词项" in report
    assert "上下文预算（字符）：1" in report
