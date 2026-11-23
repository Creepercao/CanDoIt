import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore


def corpus(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# Needle\n" + "ordinary " * 40 + "\n# Other\nelsewhere", "manual", 25)
    sid = store.save(snapshot)
    store.activate(sid)
    return store, snapshot, sid


def test_trace_explains_title_only_matches_and_top_k_loss(tmp_path):
    store, snapshot, _ = corpus(tmp_path)
    view = store.prepare()
    ids = [chunk["id"] for chunk in snapshot["chunks"] if chunk["parent_id"] == snapshot["sections"][0]["id"]]
    trace = view.relevance_trace("needle", ids, top_k=1)
    assert trace["hits"] == view.search("needle", top_k=1)
    assert {item["stage"] for item in trace["relevance_trace"]} == {"returned", "below_top_k"}
    assert all(item["title_rank"] and item["rank"] for item in trace["relevance_trace"])
    body = view.relevance_trace("needle", [ids[-1]], route="body")
    assert body["relevance_trace"][0]["stage"] == "chunk_no_match"


def test_trace_distinguishes_unmatched_sections_and_empty_query_terms(tmp_path):
    store, snapshot, _ = corpus(tmp_path)
    view = store.prepare()
    identifier = snapshot["chunks"][-1]["id"]
    assert view.relevance_trace("needle", [identifier])["relevance_trace"][0]["stage"] == "section_no_match"
    assert view.relevance_trace("😀", [identifier])["relevance_trace"][0]["stage"] == "no_query_terms"
    with pytest.raises(ValueError, match="selected corpus"):
        view.relevance_trace("needle", ["missing"])


def test_trace_identifies_section_pruning(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# A\nneedle\n# B\nneedle", "manual")
    store.activate(store.save(snapshot))
    view = store.prepare()
    result = view.relevance_trace("needle", [chunk["id"] for chunk in snapshot["chunks"]], section_k=1)
    assert sorted(item["stage"] for item in result["relevance_trace"]) == ["returned", "section_pruned"]
    assert sorted(item["section_rank"] for item in result["relevance_trace"]) == [1, 2]
