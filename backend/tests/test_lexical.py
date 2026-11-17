import pytest

from backend.lexical import tokenize, BM25Index


def test_tokenizer_handles_chinese_identifiers_numbers_and_punctuation():
    assert tokenize("退货规则 Redis_Stream 399元") == ["退", "货", "规", "则", "退货", "货规", "规则", "redis_stream", "399", "元"]
    assert tokenize("退，货") == ["退", "货"]  # No bigrams across punctuation.
    assert tokenize("😀 \n") == []
    assert tokenize("JWT jwt") == ["jwt", "jwt"]
    with pytest.raises(ValueError):
        tokenize(None)


def test_bm25_positive_idf_empty_queries_and_deterministic_ties():
    import math
    index = BM25Index([{"id": "a", "text": "refund"}, {"id": "b", "text": "shipping"}])
    assert index.search("refund")[0]["score"] == pytest.approx(math.log(2))
    assert index.search("unknown") == []
    assert index.search("😀") == []
    assert BM25Index([]).search("q") == []
    tie = BM25Index([{"id": "z", "text": "q"}, {"id": "a", "text": "q"}])
    assert [item["id"] for item in tie.search("q q")] == ["a", "z"]
    assert tie.search("q q") == tie.search("q")


def test_bm25_term_frequency_saturates_and_length_normalization_matters():
    docs = [{"id": "twice", "text": "q q"}, {"id": "once", "text": "q"}]
    hits = BM25Index(docs, b=0).search("q")
    assert 1 < hits[0]["score"] / hits[1]["score"] < 2
    docs = [{"id": "long", "text": "q filler filler"}, {"id": "short", "text": "q"}]
    assert BM25Index(docs, b=1).search("q")[0]["id"] == "short"
    hits = BM25Index(docs, b=0).search("q")
    assert hits[0]["score"] == hits[1]["score"]


@pytest.mark.parametrize("documents,k1,b", [([{"id": "a", "text": "a"}] * 2, 1, .5),
    ([{"id": "a", "text": None}], 1, .5), ([], True, .5), ([], 1, float("nan")), ([], 1, 2)])
def test_invalid_bm25_inputs_rejected(documents, k1, b):
    with pytest.raises(ValueError):
        BM25Index(documents, k1=k1, b=b)
