import hashlib
import json

import pytest

from backend.tools.hierarchy_artifact import make_artifact, unpack_artifact
from backend.tools.hierarchy_export import export_report, main
from backend.tests.test_hierarchy_labels import corpus


def experiment(tmp_path):
    store, snapshot, _ = corpus(tmp_path)
    report = export_report([{"query": "refund", "relevant_ids": [snapshot["chunks"][0]["id"]]}], store)
    rows = report.pop("rows")
    return rows, report


def test_artifact_is_self_contained_and_detached(tmp_path):
    rows, metadata = experiment(tmp_path)
    artifact = make_artifact(rows, metadata)
    reopened = json.loads(json.dumps(artifact))
    parsed_rows, parsed_meta = unpack_artifact(reopened)
    assert parsed_rows == rows and parsed_meta == metadata
    parsed_rows[0]["ranked_ids"].clear()
    parsed_meta["snapshot_ids"].clear()
    assert unpack_artifact(artifact) == (rows, metadata)


@pytest.mark.parametrize("field", ["rows", "metadata", "content_sha256", "kind"])
def test_artifact_rejects_modified_content(tmp_path, field):
    artifact = make_artifact(*experiment(tmp_path))
    if field == "rows":
        artifact["rows"][0]["ranked_ids"] = []
    elif field == "metadata":
        artifact["metadata"]["configuration"]["route"] = "body"
    else:
        artifact[field] = "wrong"
    with pytest.raises(ValueError):
        unpack_artifact(artifact)


@pytest.mark.parametrize("field,value,match", [
    ("corpus_sha256", "wrong", "corpus"),
    ("retrieval_sha256", "wrong", "retrieval"),
    ("snapshot_ids", [], "snapshot_ids"),
    ("queries", 99, "count"),
])
def test_even_rehashed_artifact_requires_consistent_identity(tmp_path, field, value, match):
    rows, metadata = experiment(tmp_path)
    metadata[field] = value
    with pytest.raises(ValueError, match=match):
        make_artifact(rows, metadata)


def test_cli_bundle_saves_metadata_and_protects_existing_result(tmp_path, monkeypatch, capsys):
    store, snapshot, sid = corpus(tmp_path)
    source, output = tmp_path / "labels.json", tmp_path / "experiment.json"
    source.write_text(json.dumps([{"query": "refund", "relevant_ids": [snapshot["chunks"][0]["id"]]}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["export", str(source), str(output), "--store", str(store.path), "--bundle"])
    main()
    summary = json.loads(capsys.readouterr().out)
    rows, metadata = unpack_artifact(json.loads(output.read_text(encoding="utf-8")))
    assert metadata["snapshot_ids"] == [sid]
    assert metadata["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert summary["rankings_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert summary["output_format"] == "hierarchy-rankings-v1"
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert output.read_bytes() == before
