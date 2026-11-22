from copy import deepcopy
import hashlib
import json
import sys

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore
from backend.tests.test_hierarchy_review import annotations
from backend.tools.hierarchy_review import main


def files(tmp_path):
    view, snapshot, left = annotations(tmp_path)
    right = deepcopy(left)
    right["rows"][0].update(answerable=False, relevant_ids=[])
    paths = [tmp_path / "left.json", tmp_path / "right.json"]
    for path, value in zip(paths, (left, right)):
        path.write_text(json.dumps(value), encoding="utf-8")
    return paths, view.snapshot_ids[0]


def test_compare_cli_pins_original_corpus_and_fingerprints_both_inputs(tmp_path, monkeypatch, capsys):
    paths, original = files(tmp_path)
    store = HierarchyStore(tmp_path / "store.sqlite")
    current = store.save(prepare_document("changed", "manual"))
    store.activate(current)
    output = tmp_path / "comparison.json"
    monkeypatch.setattr(sys, "argv", ["review", "--store", str(store.path), "compare", *map(str, paths), "--output", str(output)])
    main()
    assert json.loads(capsys.readouterr().out)["disputed_queries"] == ["first"]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["snapshot_ids"] == [original]
    for role, path in zip(("left", "right"), paths):
        assert report["inputs"][role]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert [row["id"] for row in store.list_versions() if row["active"]] == [current]
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and output.read_bytes() == before


def test_compare_rejects_pending_and_duplicate_keys_without_output(tmp_path, monkeypatch):
    paths, _ = files(tmp_path)
    output = tmp_path / "comparison.json"
    monkeypatch.setattr(sys, "argv", ["review", "--store", str(tmp_path / "store.sqlite"), "compare",
                                    *map(str, paths), "--output", str(output)])
    package = json.loads(paths[1].read_text(encoding="utf-8"))
    package["rows"][0]["reviewed"] = False
    paths[1].write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and not output.exists()
    paths[1].write_text('{"kind":1,"kind":2}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2 and not output.exists()


def test_consensus_cli_outputs_editable_labels_that_cannot_export_before_resolution(tmp_path, monkeypatch, capsys):
    from backend.tools.hierarchy_export import export_report
    from backend.tools.hierarchy_labels import validate_annotations
    paths, sid = files(tmp_path)
    output = tmp_path / "consensus.json"
    store = HierarchyStore(tmp_path / "store.sqlite")
    monkeypatch.setattr(sys, "argv", ["review", "--store", str(store.path), "consensus", *map(str, paths), "--output", str(output)])
    main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["inputs"]["left"]["sha256"] == hashlib.sha256(paths[0].read_bytes()).hexdigest()
    package = json.loads(output.read_text(encoding="utf-8"))
    view = store.prepare_versions([sid])
    assert validate_annotations(package, view, require_complete=False)["pending_queries"] == ["first"]
    with pytest.raises(ValueError, match="pending"):
        export_report(package, store)
    package["rows"][0].update(reviewed=True, answerable=False, note="人工裁决：没有答案")
    assert len(export_report(package, store)["rows"]) == 2
