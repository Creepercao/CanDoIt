import pytest

from backend.chunking import chunk_text


@pytest.mark.parametrize("text", ["", "  \n", "甲乙\n\n丙丁\n\n末尾", "汉" * 5001, "a\r\n\r\nb\r\n尾部", "😀abc" * 40])
def test_chunks_preserve_entire_input_and_offsets(text):
    chunks = chunk_text(text, max_chars=17)
    assert "".join(chunk["text"] for chunk in chunks) == text
    assert all(0 < len(chunk["text"]) <= 17 for chunk in chunks)
    assert all(text[chunk["start"]:chunk["end"]] == chunk["text"] for chunk in chunks)


def test_chunk_prefers_paragraph_end_before_hard_limit():
    assert [item["text"] for item in chunk_text("甲乙\n\n丙丁戊己", 6)] == ["甲乙\n\n", "丙丁戊己"]


@pytest.mark.parametrize("text,size", [(None, 5), ("a", 0), ("a", True), ("a", 1.5)])
def test_bad_chunk_configuration_rejected(text, size):
    with pytest.raises(ValueError):
        chunk_text(text, size)
