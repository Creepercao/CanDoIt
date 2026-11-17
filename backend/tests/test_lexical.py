import pytest

from backend.lexical import tokenize


def test_tokenizer_handles_chinese_identifiers_numbers_and_punctuation():
    assert tokenize("退货规则 Redis_Stream 399元") == ["退", "货", "规", "则", "退货", "货规", "规则", "redis_stream", "399", "元"]
    assert tokenize("退，货") == ["退", "货"]  # No bigrams across punctuation.
    assert tokenize("😀 \n") == []
    assert tokenize("JWT jwt") == ["jwt", "jwt"]
    with pytest.raises(ValueError):
        tokenize(None)
