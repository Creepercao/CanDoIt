from copy import deepcopy

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_labels import draft_annotations, validate_annotations


def corpus(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    snapshot = prepare_document("# Refund\nrefund policy", "manual")
    sid = store.save(snapshot)
    store.activate(sid)
    return store, snapshot, sid


def test_draft_is_detached_pending_and_bound_to_the_captured_corpus(tmp_path):
    store, snapshot, sid = corpus(tmp_path)
    view = store.prepare()
    questions = [{"query": "refund", "tags": ["售后"]}, {"query": "unknown"}]
    before = deepcopy(questions)
    draft = draft_annotations(questions, view)
    store.activate(store.save(prepare_document("changed", "manual")))
    assert draft["snapshot_ids"] == [sid]
    assert draft["corpus_sha256"] == view.inventory()["corpus_sha256"]
    assert draft["rows"][1]["answerable"] is None
    assert all(row["reviewed"] is False and row["relevant_ids"] == [] for row in draft["rows"])
    draft["rows"][0]["tags"].append("changed")
    assert questions == before


@pytest.mark.parametrize("questions", [[], ["q"], [{"query": " "}],
    [{"query": "q"}, {"query": "q"}], [{"query": "q", "tags": "tag"}],
    [{"query": "q", "tags": [False]}], [{"query": "q", "relevant_ids": ["fake"]}]])
def test_draft_rejects_ambiguous_question_seeds(tmp_path, questions):
    store, _, _ = corpus(tmp_path)
    with pytest.raises(ValueError):
        draft_annotations(questions, store.prepare())


def test_draft_refuses_empty_corpus(tmp_path):
    with pytest.raises(ValueError, match="contain snapshots"):
        draft_annotations([{"query": "q"}], HierarchyStore(tmp_path / "absent.sqlite").prepare())


def test_review_progress_never_promotes_pending_to_no_answer(tmp_path):
    store, snapshot, sid = corpus(tmp_path)
    view = store.prepare()
    package = draft_annotations([{"query": "refund"}, {"query": "missing"}, {"query": "pending"}], view)
    package["rows"][0].update(reviewed=True, answerable=True, relevant_ids=[snapshot["chunks"][0]["id"]])
    package["rows"][1].update(reviewed=True, answerable=False)
    before = deepcopy(package)
    progress = validate_annotations(package, view, require_complete=False)
    assert (progress["queries"], progress["reviewed_queries"], progress["pending_queries"]) == (3, 2, ["pending"])
    assert progress["rows"][1]["answerable"] is False
    assert package == before
    with pytest.raises(ValueError, match="pending review"):
        validate_annotations(package, view)
    package["rows"][2].update(reviewed=True, answerable=False)
    assert validate_annotations(package, view)["complete"] is True


@pytest.mark.parametrize("change,match", [
    ({"reviewed": True}, "explicit"),
    ({"reviewed": 1}, "boolean"),
    ({"answerable": 0}, "boolean"),
    ({"reviewed": True, "answerable": True}, "relevant IDs"),
    ({"reviewed": True, "answerable": False, "relevant_ids": ["valid"]}, "no-answer"),
    ({"relevant_ids": ["section"]}, "selected corpus"),
    ({"relevant_ids": ["valid", "valid"]}, "duplicates"),
    ({"relevant_ids": ["unknown"]}, "selected corpus"),
    ({"note": None}, "note"),
    ({"typo": True}, "fields"),
])
def test_annotation_contract_rejects_invalid_or_foreign_labels(tmp_path, change, match):
    store, snapshot, _ = corpus(tmp_path)
    view = store.prepare()
    package = draft_annotations([{"query": "q"}], view)
    mapped = {"valid": snapshot["chunks"][0]["id"], "section": snapshot["sections"][0]["id"]}
    change = deepcopy(change)
    if "relevant_ids" in change:
        change["relevant_ids"] = [mapped.get(item, item) for item in change["relevant_ids"]]
    package["rows"][0].update(change)
    with pytest.raises(ValueError, match=match):
        validate_annotations(package, view, require_complete=False)


def test_corpus_binding_also_protects_all_no_answer_datasets(tmp_path):
    store, _, sid = corpus(tmp_path)
    package = draft_annotations([{"query": "missing"}], store.prepare())
    package["rows"][0].update(reviewed=True, answerable=False)
    store.activate(store.save(prepare_document("new answer", "manual")))
    with pytest.raises(ValueError, match="corpus"):
        validate_annotations(package, store.prepare())
    historical = store.prepare_versions([sid])
    assert validate_annotations(package, historical)["complete"]
    package["corpus_sha256"] = "wrong"
    with pytest.raises(ValueError, match="corpus"):
        validate_annotations(package, historical)
