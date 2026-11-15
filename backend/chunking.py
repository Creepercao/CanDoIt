"""Offline text preparation for hierarchical retrieval; offsets use characters."""

import re


def chunk_text(text, max_chars=1000):
    """Prefer paragraph endings, hard-split long paragraphs, preserve all text."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    boundaries = [match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n", text)]
    chunks = []
    start = 0
    while start < len(text):
        limit = min(start + max_chars, len(text))
        end = limit
        if limit < len(text):
            candidates = [boundary for boundary in boundaries if start < boundary <= limit]
            if candidates:
                end = candidates[-1]
        chunks.append({"start": start, "end": end, "text": text[start:end]})
        start = end
    return chunks
