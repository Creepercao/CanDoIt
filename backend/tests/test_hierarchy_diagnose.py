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


def test_batch_diagnosis_separates_no_answer_and_counts_per_question_evidence(tmp_path):
    from backend.tools.hierarchy_labels import draft_annotations
    from backend.tools.hierarchy_diagnose import diagnose_labels
    store, snapshot, _ = corpus(tmp_path)
    view = store.prepare()
    package = draft_annotations([{"query": "needle", "tags": ["售后"]}, {"query": "ordinary", "tags": ["售后"]}], view)
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    package["rows"][1].update(reviewed=True, answerable=False)
    report = diagnose_labels(package, view)
    assert report["relevant_chunks"] == sum(report["stages"].values()) == 1
    assert report["no_answer_queries"] == report["no_answer_nonempty_queries"] == 1
    assert report["slices"]["售后"]["queries"] == 2
    assert report["snapshot_ids"] == list(view.snapshot_ids)
    package["rows"][1]["reviewed"] = False
    with pytest.raises(ValueError, match="pending"):
        diagnose_labels(package, view)


def test_markdown_diagnosis_escapes_labels_and_keeps_stage_evidence(tmp_path):
    from backend.tools.hierarchy_labels import draft_annotations
    from backend.tools.hierarchy_diagnose import diagnose_labels, render_diagnosis
    store, snapshot, sid = corpus(tmp_path)
    view = store.prepare()
    package = draft_annotations([{"query": "needle|x\ny", "tags": ["售后|分组"]}], view)
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    report = diagnose_labels(package, view)
    markdown = render_diagnosis(report)
    assert sid in markdown and report["retrieval_sha256"] in markdown
    assert "needle&#124;x<br>y" in markdown and "售后&#124;分组" in markdown
    assert "## 逐题证据" in markdown and "无答案题不产生相关分块追踪" in markdown


def test_context_diagnosis_distinguishes_returned_evidence_from_visible_evidence(tmp_path):
    from backend.tools.hierarchy_labels import draft_annotations
    from backend.tools.hierarchy_diagnose import diagnose_labels, render_diagnosis
    store, snapshot, _ = corpus(tmp_path)
    view = store.prepare()
    ids = [chunk["id"] for chunk in snapshot["chunks"] if chunk["parent_id"] == snapshot["sections"][0]["id"]]
    package = draft_annotations([{"query": "needle"}], view)
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=ids)
    small = diagnose_labels(package, view, top_k=200, context_chars=1)
    assert small["stages"]["returned"] == len(ids)
    assert small["context_states"]["complete"] == 0
    assert small["context_states"]["omitted"] > 0
    assert small["context_states"]["partial"] >= 1
    full = diagnose_labels(package, view, top_k=200, context_chars=2000)
    assert full["context_states"]["complete"] == len(ids)
    assert full["retrieval_sha256"] == small["retrieval_sha256"]
    assert "上下文预算（字符）：1" in render_diagnosis(small)
    with pytest.raises(ValueError, match="positive integer"):
        diagnose_labels(package, view, context_chars=False)
