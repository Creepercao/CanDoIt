from copy import deepcopy

import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_labels import draft_annotations


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
