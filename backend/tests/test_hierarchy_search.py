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


def test_body_route_allows_comparison_without_title_only_candidates(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    store.activate(store.save(prepare_document("# Refund\n" + "条件说明" * 100, "manual", 30)))
    view = store.prepare()
    fusion = view.search("refund", top_k=200)
    body = view.search("refund", top_k=200, route="body")
    assert len(body) == 1 < len(fusion)
    assert body[0]["score_kind"] == "bm25"
    assert body[0]["score"] == body[0]["body_score"] > 0
    assert body[0]["title_score"] == 0 and body[0]["ranks"] == [1, None]
    assert all(hit["score_kind"] == "rrf" for hit in fusion)
    with pytest.raises(ValueError, match="route"):
        view.search("refund", route="unknown")


def test_diagnostics_explain_candidate_pruning_and_empty_results(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    store.activate(store.save(prepare_document("# Refund\n" + "条件" * 70 + "\n# Shipping\ntracking", "manual", 20)))
    view = store.prepare()
    result = view.search_report("refund", top_k=2, section_k=1)
    diag = result["diagnostics"]
    assert diag["sections"] == 2
    assert len(diag["selected_sections"]) == 1
    assert diag["selected_chunks"] < diag["chunks"]
    assert diag["title_candidates"] == diag["selected_chunks"]
    assert diag["returned_hits"] == 2
    assert diag["candidate_cache_hit"] is False
    assert view.search_report("refund", top_k=2, section_k=1)["diagnostics"]["candidate_cache_hit"] is True
    assert view.search("refund", top_k=2, section_k=1) == result["hits"]
    empty = view.search_report("unrecorded")["diagnostics"]
    assert empty["selected_sections"] == [] and empty["returned_hits"] == 0
    assert empty["body_candidates"] == empty["fused_candidates"] == 0


def test_retrieval_fingerprints_track_corpus_and_configuration_not_runtime_state(tmp_path):
    store = HierarchyStore(tmp_path / "store.sqlite")
    store.activate(store.save(prepare_document("# Refund\nrefund", "manual")))
    view = store.prepare()
    first = view.retrieval_identity()
    assert first == store.prepare().retrieval_identity()
    warm = view.search_report("refund")
    assert warm["retrieval_sha256"] == first["retrieval_sha256"]
    assert view.search_report("unknown")["retrieval_sha256"] == first["retrieval_sha256"]
    for changed in (view.retrieval_identity(top_k=1), view.retrieval_identity(section_k=1), view.retrieval_identity(route="body")):
        assert changed["corpus_sha256"] == first["corpus_sha256"]
        assert changed["retrieval_sha256"] != first["retrieval_sha256"]
    first["configuration"]["bm25"]["b"] = 0
    assert view.retrieval_identity()["configuration"]["bm25"]["b"] == .75
    store.activate(store.save(prepare_document("updated refund", "manual")))
    assert store.prepare().retrieval_identity()["corpus_sha256"] != warm["corpus_sha256"]
