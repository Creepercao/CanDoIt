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
                                           (1.5, .3), (True, .3)])
def test_invalid_configuration_makes_no_calls(depth, threshold):
    async def search(*args, **kwargs):
        pytest.fail("must validate before retrieval")

    with pytest.raises(ValueError):
        asyncio.run(export_rankings([], search, depth, threshold))
