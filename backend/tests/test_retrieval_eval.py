import pytest

from backend.tools.retrieval_eval import evaluate


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


@pytest.mark.parametrize("rows,k", [([], 5), ([{}], 5),
    ([{"query": "a", "relevant_ids": [], "ranked_ids": []}], 5),
    ([{"query": "a", "relevant_ids": [1], "ranked_ids": []}], 5),
    ([{}], 0)])
def test_invalid_input_rejected(rows, k):
    with pytest.raises(ValueError):
        evaluate(rows, k)
