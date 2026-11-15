"""Offline text preparation for hierarchical retrieval; offsets use characters."""

import re
import hashlib
import json
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


def _identifier(kind, *parts):
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return kind + "-" + hashlib.sha256(payload).hexdigest()[:32]


def prepare_document(text, source, max_chars=1000, overlap_chars=0):
    """Build document/section/chunk records without embedding or persistence."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("document text must be a non-empty string")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty stable document key")
    chunk_text("", max_chars, overlap_chars)  # Validate even before section iteration.
    doc_id = _identifier("doc", source, text)
    sections, chunks = [], []
    for section in split_sections(text):
        section_id = _identifier("section", doc_id, section["start"], section["end"])
        sections.append({**section, "id": section_id, "parent_id": doc_id})
        for chunk in chunk_text(section["text"], max_chars, overlap_chars):
            start, end = section["start"] + chunk["start"], section["start"] + chunk["end"]
            chunks.append({"id": _identifier("chunk", section_id, start, end), "parent_id": section_id,
                           "document_id": doc_id, "start": start, "end": end, "text": chunk["text"]})
    total_chars = sum(len(chunk["text"]) for chunk in chunks)
    return {"version": 1, "configuration": {"max_chars": max_chars, "overlap_chars": overlap_chars},
            "document": {"id": doc_id, "source": source, "text": text,
                         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
            "sections": sections, "chunks": chunks,
            "stats": {"source_chars": len(text), "sections": len(sections), "chunks": len(chunks),
                      "chunk_chars": total_chars, "overlap_chars": total_chars - len(text)}}
