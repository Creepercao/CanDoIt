import pytest

from backend.tools.retrieval_eval import evaluate, compare
from backend.tools.retrieval_report import render_markdown


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
