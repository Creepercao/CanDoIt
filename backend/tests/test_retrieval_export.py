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


def test_export_duplicate_queries_never_call_search():
    async def search(*args, **kwargs):
        pytest.fail("duplicates must fail before retrieval")

    row = {"query": "q", "relevant_ids": ["a"]}
    with pytest.raises(ValueError, match="unique"):
        asyncio.run(export_rankings([row, row], search))


@pytest.mark.parametrize("failure", ["labels", "depth", "threshold", "existing", "parent"])
def test_cli_preflight_errors_never_initialize_knowledge_base(tmp_path, monkeypatch, failure):
    import json
    import sys
    from types import SimpleNamespace
    from backend.tools.retrieval_export import main

    def forbidden():
        pytest.fail("must not initialize providers for invalid inputs")

    monkeypatch.setitem(sys.modules, "backend.research_cache", SimpleNamespace(get_knowledge_base=forbidden))
    source, output = tmp_path / "labels.json", tmp_path / "rankings.json"
    source.write_text(json.dumps([{"query": "q", "relevant_ids": ["a"]}]), encoding="utf-8-sig")
    extra = []
    if failure == "labels":
        source.write_text('[{}]', encoding="utf-8")
    elif failure == "existing":
        output.write_text("keep", encoding="utf-8")
    elif failure == "parent":
        output = tmp_path / "missing" / "rankings.json"
    elif failure == "depth":
        extra = ["--depth", "0"]
    monkeypatch.setattr("sys.argv", ["retrieval_export", str(source), str(output),
                                    "--threshold", "nan" if failure == "threshold" else ".3", *extra])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    if failure == "existing":
        assert output.read_text(encoding="utf-8") == "keep"
    else:
        assert not output.exists()


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
    assert calls == [(query, {"top_k": 3, "threshold": .7, "strict": True})
                     for query in ("已收录", "未收录", "已收录", "未收录")]


@pytest.mark.parametrize("failure", ["embedding", "query"])
def test_real_knowledge_base_failure_does_not_export_false_empty_rankings(tmp_path, monkeypatch, failure):
    import json
    from backend import research_cache as rc
    from backend.tools.retrieval_export import main

    class Collection:
        def query(self, **kwargs):
            raise OSError("database is down")

    kb = rc.KnowledgeBase.__new__(rc.KnowledgeBase)
    kb._initialized = True
    kb._collection = Collection()

    async def embedding(*args):
        return None if failure == "embedding" else [.1, .2]

    monkeypatch.setattr(rc, "get_embedding", embedding)
    monkeypatch.setattr(rc, "get_knowledge_base", lambda: kb)
    source, output = tmp_path / "labels.json", tmp_path / "rankings.json"
    source.write_text(json.dumps([{"query": "unknown", "answerable": False, "relevant_ids": []}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["retrieval_export", str(source), str(output), "--threshold", ".3"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not output.exists()


def test_timeout_cancels_pending_search_and_never_queries_later_rows():
    calls, cancelled = [], []

    async def search(query, **kwargs):
        calls.append(query)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(query)

    labels = [{"query": query, "relevant_ids": ["a"]} for query in ("first", "second")]
    with pytest.raises(ValueError, match="timed out at row 1"):
        asyncio.run(export_rankings(labels, search, timeout_seconds=.01))
    assert calls == cancelled == ["first"]
    assert all("ranked_ids" not in row for row in labels)


@pytest.mark.parametrize("timeout", [0, -1, True, None, "1", float("inf"), float("nan")])
def test_invalid_timeout_never_calls_retrieval(timeout):
    async def search(*args, **kwargs):
        pytest.fail("invalid timeout must fail before retrieval")

    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(export_rankings([{"query": "q", "relevant_ids": ["a"]}], search, timeout_seconds=timeout))
