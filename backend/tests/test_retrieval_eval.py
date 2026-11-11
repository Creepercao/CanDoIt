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
