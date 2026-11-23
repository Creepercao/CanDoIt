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
