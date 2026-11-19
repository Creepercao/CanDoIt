import pytest

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyStore


def test_two_stage_lexical_search_uses_active_versions_and_resolves_context(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    old = store.save(prepare_document("# old\noldunique refund", "manual"))
    store.activate(old)
    text = "# Shipping\n物流运单\n# Returns\n退款退货 refund policy\n" + "条件说明\n" * 20
    snapshot = prepare_document(text, "manual", 40, 5)
    store.activate(store.save(snapshot))
    store.save(prepare_document("# draft\noldunique refund", "draft"))
    assert store.search("oldunique") == []
    hits = store.search("refund", top_k=3, section_k=1)
    assert hits and all(hit["source"] == "manual" for hit in hits)
    assert all(hit["path"] == ["Returns"] for hit in hits)
    assert hits[0]["body_score"] > 0
    assert all(text[hit["start"]:hit["end"]] == hit["text"] for hit in hits)
    context = store.context([hit["id"] for hit in hits])
    assert "refund policy" in context["contexts"][0]["text"]
    assert store.search("neverrecorded") == []
    assert store.search("😀") == []


def test_title_route_can_retrieve_chunks_without_literal_query_in_body(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    store.activate(store.save(prepare_document("# Refund\n" + "条件说明" * 60, "policy", 20)))
    hits = store.search("refund", top_k=200)
    assert any(hit["body_score"] == 0 and hit["title_score"] > 0 for hit in hits)
    assert all(hit["ranks"][1] is not None for hit in hits)


@pytest.mark.parametrize("query,k,section_k", [("", 5, 5), (None, 5, 5), ("q", True, 5), ("q", 5, 0), ("q", 201, 5)])
def test_search_configuration_rejected_before_creating_store(tmp_path, query, k, section_k):
    path = tmp_path / "store.sqlite"
    with pytest.raises(ValueError):
        HierarchyStore(path).search(query, k, section_k)
    assert not path.exists()


def test_source_filter_is_applied_before_ranking_and_context_expansion(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    for source in ("manual", "policy"):
        store.activate(store.save(prepare_document("needle " + source, source)))
    hits = store.search("needle", sources=["manual", "manual"])
    assert hits and {hit["source"] for hit in hits} == {"manual"}
    policy_hit = store.search("needle", sources=["policy"])[0]
    with pytest.raises(ValueError):
        store.context([policy_hit["id"]], sources=["manual"])
    with pytest.raises(ValueError, match="no active"):
        store.prepare(["typo"])
    assert len(store.prepare().snapshot_ids) == 2


@pytest.mark.parametrize("sources", [[], "manual", [None], [""], [True]])
def test_invalid_source_filters_never_create_database(tmp_path, sources):
    path = tmp_path / "store.sqlite"
    with pytest.raises(ValueError):
        HierarchyStore(path).prepare(sources)
    assert not path.exists()


def test_queries_reuse_prepared_records_and_results_cannot_mutate_the_view(tmp_path, monkeypatch):
    store = HierarchyStore(tmp_path / "store.sqlite")
    store.activate(store.save(prepare_document("# Refund\nrefund policy", "manual")))
    view = store.prepare()
    monkeypatch.setattr(view, "active_snapshots", lambda: pytest.fail("query must reuse prepared records"))
    hit = view.search("refund")[0]
    hit["path"].append("tampered")
    assert view.search("refund")[0]["path"] == ["Refund"]
    context = view.context([hit["id"]])
    context["contexts"][0]["path"].append("tampered")
    assert view.context([hit["id"]])["contexts"][0]["path"] == ["Refund"]


def test_bm25_indexes_are_reused_and_candidate_cache_is_bounded(tmp_path, monkeypatch):
    from backend import hierarchy
    store = HierarchyStore(tmp_path / "store.sqlite")
    for word in ("alpha", "beta", "gamma", "delta", "epsilon"):
        store.activate(store.save(prepare_document(word, word)))
    actual, builds = hierarchy.BM25Index, []

    def counted(documents, *args, **kwargs):
        builds.append(len(documents))
        return actual(documents, *args, **kwargs)

    monkeypatch.setattr(hierarchy, "BM25Index", counted)
    view = store.prepare()
    first = view.search("alpha", section_k=1)
    assert len(builds) == 3  # One section index and the two candidate routes.
    assert view.search("alpha", section_k=1) == first
    assert len(builds) == 3
    for word in ("beta", "gamma", "delta", "epsilon"):
        view.search(word, section_k=1)
    assert len(view._candidate_indexes) == hierarchy.CANDIDATE_CACHE_LIMIT
    assert view.search("alpha", section_k=1) == first  # Rebuilt after eviction, same ranking.
    assert len(view._candidate_indexes) == hierarchy.CANDIDATE_CACHE_LIMIT
