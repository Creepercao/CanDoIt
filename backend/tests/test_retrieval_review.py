import pytest

from backend.tools.retrieval_eval import evaluate, compare
from backend.tools.retrieval_report import render_markdown
from backend.tools.retrieval_review import build_review


def test_diagnostics_distinguish_cutoff_misses_from_missing_rankings():
    row = {"query": "q", "relevant_ids": ["c", "b", "a", "a"], "ranked_ids": ["x", "x", "b", "a"]}
    result = evaluate([row], k=2)
    detail = result["details"][0]
    assert detail["hit_ids"] == ["b"]
    assert detail["missed_ids"] == ["a", "c"]
    assert detail["ranked_ids"] == ["x", "b", "a"]
    assert row["ranked_ids"] == ["x", "x", "b", "a"]
    report = render_markdown(result)
    assert "| q | a；b；c | b | a；c | x；b；a |" in report


def test_no_answer_diagnostics_keep_irrelevant_ranked_ids():
    detail = evaluate([{"query": "q", "answerable": False, "relevant_ids": [], "ranked_ids": ["x"]}])["details"][0]
    assert detail["hit_ids"] == detail["missed_ids"] == []
    assert detail["ranked_ids"] == ["x"]


def test_review_keeps_metric_tradeoffs_and_both_rankings():
    baseline = [{"query": "tradeoff", "relevant_ids": ["a", "b"], "ranked_ids": ["a"], "tags": ["售后"]},
                {"query": "unknown", "answerable": False, "relevant_ids": [], "ranked_ids": []}]
    candidate = [{**baseline[1], "ranked_ids": ["x"]},
                 {**baseline[0], "ranked_ids": ["x", "a", "b"]}]
    comparison = compare(baseline, candidate)
    review = build_review(comparison)
    assert review["selected_queries"] == 2
    assert review["cases"][0]["regressed_metrics"] == ["mrr@5", "mrr"]
    assert review["cases"][0]["baseline"]["ranked_ids"] == ["a"]
    assert review["cases"][0]["candidate"]["ranked_ids"] == ["x", "a", "b"]
    assert review["cases"][0]["changes"]["recall_delta"] == .5
    assert review["cases"][0]["review_note"] == ""
    assert build_review(comparison, tag="售后")["selected_queries"] == 1
    assert build_review(comparison, metric="recall@5")["cases"] == []
    with pytest.raises(ValueError):
        build_review(comparison, metric="typo")
    with pytest.raises(ValueError):
        build_review(comparison, tag="typo")
