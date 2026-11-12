import pytest

from backend.tools.retrieval_eval import evaluate
from backend.tools.retrieval_eval import compare


def test_explicit_no_answer_labels_do_not_dilute_retrieval_recall():
    result = evaluate([
        {"query": "known", "relevant_ids": ["a"], "ranked_ids": ["a"]},
        {"query": "unknown-empty", "answerable": False, "relevant_ids": [], "ranked_ids": []},
        {"query": "unknown-returned", "answerable": False, "relevant_ids": [], "ranked_ids": ["x"]},
    ])
    assert result["recall@5"] == result["mrr"] == 1
    assert result["answerable_queries"] == 1
    assert result["no_answer_empty_rate"] == result["no_answer_retrieval_rate"] == .5
    assert result["details"][1]["recall"] is None


def test_all_no_answer_corpus_has_undefined_recall():
    result = evaluate([{"query": "unknown", "answerable": False, "relevant_ids": [], "ranked_ids": []}])
    assert result["recall@5"] is result["mrr"] is None
    assert result["no_answer_empty_rate"] == 1


def test_compare_no_answer_metrics_and_null_denominators():
    baseline = [{"query": "unknown", "answerable": False, "relevant_ids": [], "ranked_ids": ["x"]}]
    candidate = [{**baseline[0], "ranked_ids": []}]
    result = compare(baseline, candidate)
    assert result["delta"]["mrr"] is None
    assert result["delta"]["no_answer_empty_rate"] == 1
    assert result["delta"]["no_answer_retrieval_rate"] == -1


def test_compare_legacy_labels_match_explicit_answerable_true():
    baseline = [{"query": "known", "relevant_ids": ["a"], "ranked_ids": ["a"]}]
    assert compare(baseline, [{**baseline[0], "answerable": True}])["delta"]["recall@5"] == 0


def test_query_changes_align_reordered_queries_and_expose_metric_tradeoffs():
    baseline = [
        {"query": "known", "relevant_ids": ["a", "b"], "ranked_ids": ["a"]},
        {"query": "unknown", "answerable": False, "relevant_ids": [], "ranked_ids": ["x"]},
    ]
    candidate = [{**baseline[1], "ranked_ids": []},
                 {**baseline[0], "ranked_ids": ["x", "a", "b"]}]
    changes = compare(baseline, candidate, k=3)["query_changes"]
    assert changes == [
        {"query": "known", "answerable": True, "recall_delta": .5, "hit_delta": 0,
         "reciprocal_rank_delta": -.5, "reciprocal_rank_at_k_delta": -.5, "empty_retrieval_delta": None},
        {"query": "unknown", "answerable": False, "recall_delta": None, "hit_delta": None,
         "reciprocal_rank_delta": None, "reciprocal_rank_at_k_delta": None, "empty_retrieval_delta": 1},
    ]
    unchanged = compare(baseline, baseline, k=3)["query_changes"]
    assert unchanged[0]["recall_delta"] == unchanged[0]["reciprocal_rank_delta"] == 0
    assert unchanged[1]["empty_retrieval_delta"] == 0


@pytest.mark.parametrize("answerable,ids", [(False, ["a"]), (True, []), ("false", []), (0, [])])
def test_invalid_answerability_contract_rejected(answerable, ids):
    with pytest.raises(ValueError):
        evaluate([{"query": "q", "answerable": answerable, "relevant_ids": ids, "ranked_ids": []}])


def test_compare_scores_and_rejects_different_labels():
    before = [{"query": "a", "relevant_ids": ["a"], "ranked_ids": []}]
    after = [{"query": "a", "relevant_ids": ["a"], "ranked_ids": ["a"]}]
    assert compare(before, after)["delta"]["mrr"] == 1
    with pytest.raises(ValueError):
        compare(before, [{**after[0], "relevant_ids": ["b"]}])
    with pytest.raises(ValueError):
        compare(before * 2, after * 2)


def test_comparison_cli(tmp_path, monkeypatch, capsys):
    import json
    from backend.tools.retrieval_eval import main

    rows = [{"query": "a", "relevant_ids": ["a"], "ranked_ids": ["a"]}]
    path = tmp_path / "rankings.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(path), "--baseline", str(path)])
    main()
    assert json.loads(capsys.readouterr().out)["delta"]["mrr"] == 0


def test_cli_saves_utf8_report_and_preserves_existing_output(tmp_path, monkeypatch, capsys):
    import json
    from backend.tools.retrieval_eval import main

    source = tmp_path / "rankings.json"
    output = tmp_path / "report.json"
    source.write_text(json.dumps([
        {"query": "没有收录的商品", "answerable": False, "relevant_ids": [], "ranked_ids": []}
    ]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(source), "--output", str(output)])
    main()
    saved = output.read_text(encoding="utf-8")
    assert "没有收录的商品" in saved
    assert json.loads(saved) == json.loads(capsys.readouterr().out)
    assert json.loads(saved)["mrr"] is None

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert output.read_text(encoding="utf-8") == saved
    assert capsys.readouterr().out == ""


def test_cli_invalid_labels_do_not_create_report(tmp_path, monkeypatch):
    from backend.tools.retrieval_eval import main

    source = tmp_path / "bad.json"
    output = tmp_path / "report.json"
    source.write_text('[{"query":"q","relevant_ids":[],"ranked_ids":[]}]', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(source), "--output", str(output)])
    with pytest.raises(SystemExit):
        main()
    assert not output.exists()


@pytest.mark.parametrize("rows,k", [("bad", 5), ([None], 5), ([{}], 1.5), ([{}], True)])
def test_public_api_rejects_wrong_types(rows, k):
    with pytest.raises(ValueError):
        evaluate(rows, k)


def test_metrics_include_misses_multiple_labels_and_deduplicate():
    result = evaluate([
        {"query": "a", "relevant_ids": ["a", "b"],
         "ranked_ids": ["x", "x", "a", "b"]},
        {"query": "b", "relevant_ids": ["c"], "ranked_ids": []},
    ], k=2)
    assert result["recall@2"] == 0.25
    assert result["mrr"] == 0.25


def test_mrr_uses_full_ranking():
    result = evaluate([{"query": "a", "relevant_ids": ["a"],
                        "ranked_ids": ["x", "a"]}], k=1)
    assert result["recall@1"] == 0
    assert result["mrr"] == 0.5
    assert result["hit_rate@1"] == 0
    assert result["mrr@1"] == 0


def test_cutoff_mrr_counts_boundary_after_deduplication_and_compares():
    rows = [{"query": "q", "relevant_ids": ["a"], "ranked_ids": ["x", "x", "a"]}]
    result = evaluate(rows, k=2)
    assert result["mrr@2"] == .5
    assert result["details"][0]["reciprocal_rank_at_k"] == .5
    comparison = compare(rows, [{**rows[0], "ranked_ids": ["x", "y", "a"]}], k=2)
    assert comparison["delta"]["mrr@2"] == -.5
    assert comparison["query_changes"][0]["reciprocal_rank_at_k_delta"] == -.5


def test_cutoff_mrr_no_answer_and_miss_denominators():
    unsupported = {"query": "unknown", "answerable": False, "relevant_ids": [], "ranked_ids": []}
    assert evaluate([unsupported])["mrr@5"] is None
    result = evaluate([unsupported, {"query": "known", "relevant_ids": ["a"], "ranked_ids": []}])
    assert result["mrr@5"] == 0
    assert result["details"][0]["reciprocal_rank_at_k"] is None


def test_overlapping_slices_have_separate_denominators_and_stable_comparison():
    rows = [
        {"query": "退款", "relevant_ids": ["a"], "ranked_ids": ["a"], "tags": ["售后", "售后", "多意图"]},
        {"query": "未收录", "answerable": False, "relevant_ids": [], "ranked_ids": ["x"], "tags": ["售后"]},
        {"query": "未分组", "relevant_ids": ["a"], "ranked_ids": []},
    ]
    result = evaluate(rows)
    assert result["queries"] == 3
    assert result["recall@5"] == .5
    assert result["slices"]["售后"]["queries"] == 2
    assert result["slices"]["售后"]["recall@5"] == 1
    assert result["slices"]["售后"]["no_answer_empty_rate"] == 0
    assert result["slices"]["多意图"]["queries"] == 1
    assert result["slices"]["多意图"]["no_answer_empty_rate"] is None
    candidate = [rows[2], {**rows[1], "ranked_ids": []}, {**rows[0], "tags": ["多意图", "售后"]}]
    assert compare(rows, candidate)["slices"]["售后"]["delta"]["no_answer_empty_rate"] == 1
    with pytest.raises(ValueError, match="tags"):
        compare(rows, [*rows[:2], {**rows[2], "tags": ["新增"]}])


@pytest.mark.parametrize("tags", [None, "售后", [""], [3], ["   "]])
def test_invalid_slice_tags_rejected(tags):
    with pytest.raises(ValueError, match="tags"):
        evaluate([{"query": "q", "relevant_ids": ["a"], "ranked_ids": [], "tags": tags}])


def test_hit_rate_differs_from_recall_for_multiple_labels():
    result = evaluate([{"query": "a", "relevant_ids": ["a", "b"],
                        "ranked_ids": ["a"]}])
    assert result["hit_rate@5"] == 1
    assert result["recall@5"] == 0.5


@pytest.mark.parametrize("rows,k", [([], 5), ([{}], 5),
    ([{"query": "a", "relevant_ids": [], "ranked_ids": []}], 5),
    ([{"query": "a", "relevant_ids": [1], "ranked_ids": []}], 5),
    ([{}], 0)])
def test_invalid_input_rejected(rows, k):
    with pytest.raises(ValueError):
        evaluate(rows, k)
