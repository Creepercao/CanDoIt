"""Offline text preparation for hierarchical retrieval; offsets use characters."""

import re
from bisect import bisect_right


def split_sections(text):
    """Split ATX Markdown headings, ignoring headings inside fenced code."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    sections, headings = [], []
    start, offset = 0, 0
    path, title, level = [], "", 0
    fence = None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", body):
                fence = None
        else:
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", body)
            heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$", body)
            if marker:
                fence = (marker[1][0], len(marker[1]))
            elif heading:
                if offset > start:
                    sections.append({"start": start, "end": offset, "title": title,
                                     "level": level, "path": path, "text": text[start:offset]})
                level = len(heading[1])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2] or "").strip()
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, title))
                path = [item[1] for item in headings]
                start = offset
        offset += len(line)
    if start < len(text):
        sections.append({"start": start, "end": len(text), "title": title,
                         "level": level, "path": path, "text": text[start:]})
    return sections


def chunk_text(text, max_chars=1000, overlap_chars=0):
    """Prefer paragraph endings, hard-split long paragraphs, preserve all text."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if type(overlap_chars) is not int or not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be an integer from 0 to max_chars - 1")
    boundaries = [match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n", text)]
    chunks = []
    start = 0
    while start < len(text):
        limit = min(start + max_chars, len(text))
        end = limit
        if limit < len(text):
            index = bisect_right(boundaries, limit) - 1
            if index >= 0 and boundaries[index] > start + overlap_chars:
                end = boundaries[index]
        chunks.append({"start": start, "end": end, "text": text[start:end]})
        if end == len(text):
            break
        start = end - overlap_chars
    return chunks
