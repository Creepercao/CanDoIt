import pytest

from backend.chunking import chunk_text, split_sections


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


def test_sections_keep_preamble_and_heading_ancestry_without_losing_text():
    text = "前言\r\n# 商品\r\n介绍\r\n### 用法\r\n方法\r\n## 售后 ##\r\n尾部"
    sections = split_sections(text)
    assert [item["path"] for item in sections] == [[], ["商品"], ["商品", "用法"], ["商品", "售后"]]
    assert "".join(item["text"] for item in sections) == text
    assert all(text[item["start"]:item["end"]] == item["text"] for item in sections)


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_code_fences_and_short_closing_markers_do_not_create_sections(fence):
    text = f"# A\n{fence}python\n# fake\n{fence[:2]}\n## still fake\n{fence}\n# B\nend"
    assert [item["title"] for item in split_sections(text)] == ["A", "B"]


def test_unclosed_code_fence_keeps_tail_and_duplicate_headings_are_distinct():
    text = "# A\n# A\n```\n# literal\ntail"
    result = split_sections(text)
    assert len(result) == 2
    assert result[0]["start"] != result[1]["start"]
    assert "".join(item["text"] for item in result) == text
    assert split_sections("") == []
