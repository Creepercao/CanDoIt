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


def test_export_refuses_empty_corpus_and_freezes_versions_during_queries(tmp_path, monkeypatch):
    store = HierarchyStore(tmp_path / "store.sqlite")
    labels = [{"query": "q", "relevant_ids": ["a"]}]
    with pytest.raises(ValueError, match="no active"):
        export_rankings(labels, store)
    first = store.save(prepare_document("q", "manual"))
    second = store.save(prepare_document("new q", "manual"))
    store.activate(first)
    labels[0]["relevant_ids"] = [store.get(first)["chunks"][0]["id"]]
    view = store.prepare()
    original = view.search

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        store.activate(second)
        return result

    monkeypatch.setattr(view, "search", changed)
    monkeypatch.setattr(store, "prepare", lambda: view)
    rows, active = export_rankings(labels + [{**labels[0], "query": "q q"}], store)
    assert active == [first]
    assert rows[0]["ranked_ids"] == [store.get(first)["chunks"][0]["id"]]
    assert rows[1]["ranked_ids"] == rows[0]["ranked_ids"]


def test_export_respects_source_scope_and_records_selected_ids(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    selected = None
    for source in ("manual", "policy"):
        sid = store.save(prepare_document("needle", source))
        store.activate(sid)
        if source == "manual":
            selected = sid
    labels = [{"query": "needle", "relevant_ids": [store.get(selected)["chunks"][0]["id"]]}]
    rows, active = export_rankings(labels, store, sources=["manual"])
    assert active == [selected]
    assert rows[0]["ranked_ids"] == labels[0]["relevant_ids"]
    with pytest.raises(ValueError, match="no active"):
        export_rankings(labels, store, sources=["missing"])


def test_route_exports_can_be_compared_with_existing_metrics(tmp_path, monkeypatch, capsys):
    import hashlib
    from backend.tools.retrieval_eval import compare
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# Refund\n" + "条件" * 80, "manual", 20)
    store.activate(store.save(snapshot))
    labels = [{"query": "refund", "relevant_ids": [snapshot["chunks"][-1]["id"]]}]
    source = tmp_path / "labels.json"
    source.write_text(json.dumps(labels), encoding="utf-8")
    exports, metadata = [], []
    for route in ("body", "rrf"):
        path = tmp_path / (route + ".json")
        monkeypatch.setattr("sys.argv", ["hierarchy_export", str(source), str(path), "--store", str(store.path),
                                        "--route", route, "--depth", "200"])
        main()
        meta = json.loads(capsys.readouterr().out)
        assert meta["rankings_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert meta["configuration"]["route"] == route
        exports.append(json.loads(path.read_text(encoding="utf-8")))
        metadata.append(meta)
    assert compare(*exports, k=200)["delta"]["recall@200"] == 1
    assert metadata[0]["corpus_sha256"] == metadata[1]["corpus_sha256"]
    assert metadata[0]["retrieval_sha256"] != metadata[1]["retrieval_sha256"]


def test_cli_pins_historical_export_without_reactivating_it(tmp_path, monkeypatch, capsys):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("oldneedle", "manual")
    old = store.save(snapshot)
    new = store.save(prepare_document("newneedle", "manual"))
    store.activate(new)
    labels = tmp_path / "labels.json"
    output = tmp_path / "rankings.json"
    labels.write_text(json.dumps([{"query": "oldneedle", "relevant_ids": [snapshot["chunks"][0]["id"]]}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["hierarchy_export", str(labels), str(output), "--store", str(store.path),
                                    "--snapshot-id", old])
    main()
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["selection"] == "snapshots"
    assert metadata["selected_snapshot_ids"] == [old]
    assert "active_snapshot_ids" not in metadata
    assert json.loads(output.read_text(encoding="utf-8"))[0]["ranked_ids"] == [snapshot["chunks"][0]["id"]]
    assert [row["id"] for row in store.list_versions() if row["active"]] == [new]
    with pytest.raises(ValueError, match="mutually exclusive"):
        export_rankings([{"query": "oldneedle", "relevant_ids": [snapshot["chunks"][0]["id"]]}],
                        store, sources=["manual"], snapshot_ids=[old])


@pytest.mark.parametrize("invalid", ["unknown", "section", "old"])
def test_all_relevance_ids_checked_before_any_query(tmp_path, monkeypatch, invalid):
    store = HierarchyStore(tmp_path / "store.sqlite")
    old = prepare_document("oldneedle", "manual")
    store.save(old)
    current = prepare_document("needle", "manual")
    store.activate(store.save(current))
    bad = {"unknown": "fake", "section": current["sections"][0]["id"], "old": old["chunks"][0]["id"]}[invalid]
    labels = [{"query": "known", "relevant_ids": [current["chunks"][0]["id"]]},
              {"query": "invalid", "relevant_ids": [bad]}]
    view = store.prepare()
    monkeypatch.setattr(view, "search", lambda *a, **kw: pytest.fail("must check all IDs before retrieval"))
    monkeypatch.setattr(store, "prepare", lambda: view)
    with pytest.raises(ValueError, match="selected corpus"):
        export_rankings(labels, store)
