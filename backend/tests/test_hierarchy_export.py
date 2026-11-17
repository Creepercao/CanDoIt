import json

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_export import export_rankings, main
from backend.tools.retrieval_eval import evaluate, main as evaluate_main


def test_local_rankings_feed_existing_evaluator_and_keep_no_answer_labels(tmp_path, monkeypatch, capsys):
    path = tmp_path / "store.sqlite"
    store = HierarchyStore(path)
    snapshot = prepare_document("# Refund\nrefund policy", "manual")
    active = store.save(snapshot)
    store.activate(active)
    labels = [{"query": "refund", "relevant_ids": [snapshot["chunks"][0]["id"]], "tags": ["售后"]},
              {"query": "unrecorded", "answerable": False, "relevant_ids": []}]
    source, output = tmp_path / "labels.json", tmp_path / "rankings.json"
    source.write_text(json.dumps(labels), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["hierarchy_export", str(source), str(output), "--store", str(path), "--section-k", "1"])
    main()
    assert json.loads(capsys.readouterr().out)["active_snapshot_ids"] == [active]
    rows = json.loads(output.read_text(encoding="utf-8"))
    assert rows[1] == {**labels[1], "ranked_ids": []}
    assert evaluate(rows)["mrr@5"] == evaluate(rows)["no_answer_empty_rate"] == 1
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(output)])
    evaluate_main()
    assert json.loads(capsys.readouterr().out)["slices"]["售后"]["recall@5"] == 1
    assert "ranked_ids" not in labels[0]


def test_export_refuses_empty_or_changed_active_corpus(tmp_path, monkeypatch):
    store = HierarchyStore(tmp_path / "store.sqlite")
    labels = [{"query": "q", "relevant_ids": ["a"]}]
    with pytest.raises(ValueError, match="no active"):
        export_rankings(labels, store)
    first = store.save(prepare_document("q", "manual"))
    second = store.save(prepare_document("new q", "manual"))
    store.activate(first)
    original = store.search

    def changed(*args):
        result = original(*args)
        store.activate(second)
        return result

    monkeypatch.setattr(store, "search", changed)
    with pytest.raises(ValueError, match="changed"):
        export_rankings(labels, store)
