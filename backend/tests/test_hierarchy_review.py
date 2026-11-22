from copy import deepcopy

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_labels import draft_annotations
from backend.tools.hierarchy_review import compare_annotations, consensus_annotations


def annotations(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# A\nfirst\n# B\nsecond", "manual", 20)
    store.activate(store.save(snapshot))
    view = store.prepare()
    package = draft_annotations([{"query": "first", "tags": ["售后"]}, {"query": "unknown"}], view)
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    package["rows"][1].update(reviewed=True, answerable=False)
    return view, snapshot, package


def test_comparison_matches_queries_by_identity_and_preserves_evidence(tmp_path):
    view, snapshot, left = annotations(tmp_path)
    right = deepcopy(left)
    right["rows"][0]["relevant_ids"].append(snapshot["chunks"][-1]["id"])
    right["rows"][0]["note"] = "还需要第二章"
    right["rows"].reverse()
    before = deepcopy((left, right))
    report = compare_annotations(left, right, view)
    assert report["disputed_queries"] == ["first"]
    assert report["exact_agreements"] == 1
    assert report["answerable_agreements"] == 2
    assert report["jaccard_queries"] == 1 and report["mean_relevance_jaccard"] == .5
    assert report["details"][0]["right"]["note"] == "还需要第二章"
    assert (left, right) == before


def test_all_no_answer_is_exact_agreement_with_undefined_overlap(tmp_path):
    view, _, package = annotations(tmp_path)
    for row in package["rows"]:
        row.update(answerable=False, relevant_ids=[])
    report = compare_annotations(package, package, view)
    assert report["exact_agreements"] == 2
    assert report["jaccard_queries"] == 0 and report["mean_relevance_jaccard"] is None


@pytest.mark.parametrize("problem", ["pending", "corpus", "tags", "query"])
def test_comparison_rejects_incomparable_labels(tmp_path, problem):
    view, _, left = annotations(tmp_path)
    right = deepcopy(left)
    if problem == "pending":
        right["rows"][0]["reviewed"] = False
    elif problem == "corpus":
        right["corpus_sha256"] = "wrong"
    elif problem == "tags":
        right["rows"][0]["tags"] = ["other"]
    else:
        right["rows"][0]["query"] = "other"
    with pytest.raises(ValueError):
        compare_annotations(left, right, view)


def test_consensus_keeps_agreements_and_resets_conflicting_answerability(tmp_path):
    from backend.tools.hierarchy_labels import validate_annotations
    view, _, left = annotations(tmp_path)
    right = deepcopy(left)
    right["rows"][0].update(answerable=False, relevant_ids=[], note="未找到依据")
    result = consensus_annotations(left, right, view)
    assert result["rows"][0]["answerable"] is None
    assert result["rows"][0]["relevant_ids"] == []
    assert not result["rows"][0]["reviewed"]
    assert "未找到依据" in result["rows"][0]["note"]
    assert result["rows"][1]["reviewed"] and result["rows"][1]["answerable"] is False
    assert validate_annotations(result, view, require_complete=False)["pending_queries"] == ["first"]
    with pytest.raises(ValueError, match="pending"):
        validate_annotations(result, view)


def test_full_agreement_produces_a_complete_detached_label_package(tmp_path):
    from backend.tools.hierarchy_labels import validate_annotations
    view, _, package = annotations(tmp_path)
    result = consensus_annotations(package, package, view)
    assert validate_annotations(result, view)["complete"]
    result["rows"][0]["relevant_ids"].clear()
    assert package["rows"][0]["relevant_ids"]
