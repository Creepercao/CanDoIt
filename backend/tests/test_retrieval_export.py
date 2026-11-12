import asyncio

import pytest

from backend.tools.retrieval_export import export_rankings
from backend.tools.retrieval_eval import evaluate


def test_export_preserves_labels_order_and_scores():
    calls = []

    async def search(query, **kwargs):
        calls.append((query, kwargs))
        return [{"id": "other"}, {"id": "target"}] if query == "first" else []

    labels = [{"query": query, "relevant_ids": ["target"]}
              for query in ("first", "second")]
    rows = asyncio.run(export_rankings(labels, search, 10, 0.7))
    assert calls == [(query, {"top_k": 10, "threshold": 0.7})
                     for query in ("first", "second")]
    assert rows[1]["ranked_ids"] == []
    assert evaluate(rows)["mrr"] == 0.25
    assert "ranked_ids" not in labels[0]


@pytest.mark.parametrize("depth,threshold", [(0, .3), (201, .3), (5, float("nan")),
                                           (1.5, .3), (True, .3), (5, None),
                                           (5, True), (5, "0.3")])
def test_invalid_configuration_makes_no_calls(depth, threshold):
    async def search(*args, **kwargs):
        pytest.fail("must validate before retrieval")

    with pytest.raises(ValueError):
        asyncio.run(export_rankings([], search, depth, threshold))


def test_export_no_answer_labels_and_reject_invalid_corpus_before_search():
    calls = []

    async def search(query, **kwargs):
        calls.append(query)
        return []

    labels = [{"query": "known", "relevant_ids": ["a"]},
              {"query": "unknown", "answerable": False, "relevant_ids": []}]
    rows = asyncio.run(export_rankings(labels, search))
    assert rows[1] == {**labels[1], "ranked_ids": []}
    assert "answerable" not in rows[0]
    assert evaluate(rows)["no_answer_empty_rate"] == 1
    assert calls == ["known", "unknown"]
    assert all("ranked_ids" not in row for row in labels)

    calls.clear()
    with pytest.raises(ValueError):
        asyncio.run(export_rankings([labels[0], {**labels[1], "answerable": True}], search))
    assert calls == []


def test_export_preserves_business_tags_and_validates_them_before_search():
    calls = []

    async def search(query, **kwargs):
        calls.append(query)
        return [{"id": "a"}]

    labels = [{"query": "q", "relevant_ids": ["a"], "tags": ["售后", "长尾"]}]
    rows = asyncio.run(export_rankings(labels, search))
    assert rows[0]["tags"] == labels[0]["tags"]
    assert evaluate(rows)["slices"]["长尾"]["mrr"] == 1
    calls.clear()
    with pytest.raises(ValueError, match="tags"):
        asyncio.run(export_rankings([labels[0], {**labels[0], "tags": "售后"}], search))
    assert calls == []


def test_export_and_compare_cli_file_workflow(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace
    from backend.tools.retrieval_export import main as export_main
    from backend.tools.retrieval_eval import main as evaluate_main

    calls = []

    class FakeKnowledgeBase:
        available = False
        candidate = False

        async def _ensure_init(self):
            self.available = True

        async def search(self, query, **kwargs):
            assert self.available
            calls.append((query, kwargs))
            ids = (["a"] if query == "已收录" else []) if self.candidate else (
                ["x", "a"] if query == "已收录" else ["x"])
            return [{"id": value} for value in ids]

    kb = FakeKnowledgeBase()
    monkeypatch.setitem(sys.modules, "backend.research_cache",
                        SimpleNamespace(get_knowledge_base=lambda: kb))
    labels = [{"query": "已收录", "relevant_ids": ["a"]},
              {"query": "未收录", "answerable": False, "relevant_ids": []}]
    source = tmp_path / "labels.json"
    source.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    baseline, candidate = tmp_path / "baseline.json", tmp_path / "candidate.json"
    for path, is_candidate in ((baseline, False), (candidate, True)):
        kb.candidate = is_candidate
        monkeypatch.setattr("sys.argv", ["retrieval_export", str(source), str(path),
                                        "--depth", "3", "--threshold", "0.7"])
        export_main()

    report = tmp_path / "comparison.json"
    monkeypatch.setattr("sys.argv", ["retrieval_eval", str(candidate), "--baseline", str(baseline),
                                    "--k", "1", "--output", str(report)])
    evaluate_main()
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result == json.loads(capsys.readouterr().out)
    assert result["delta"]["recall@1"] == 1
    assert result["delta"]["mrr"] == .5
    assert result["delta"]["no_answer_empty_rate"] == 1
    assert result["query_changes"][1]["empty_retrieval_delta"] == 1
    assert json.loads(source.read_text(encoding="utf-8")) == labels
    assert calls == [(query, {"top_k": 3, "threshold": .7})
                     for query in ("已收录", "未收录", "已收录", "未收录")]
