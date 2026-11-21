"""Human relevance annotations bound to an immutable hierarchy corpus."""

from copy import deepcopy


def _questions(questions):
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty array of objects")
    seen = set()
    for row in questions:
        if not isinstance(row, dict) or set(row) - {"query", "tags"}:
            raise ValueError("questions accept only query and tags")
        query = row.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("each question needs a non-empty query")
        if query in seen:
            raise ValueError("questions must have unique queries")
        seen.add(query)
        tags = row.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ValueError("tags must be a list of non-empty strings")
    return deepcopy(questions)


def draft_annotations(questions, view):
    """Create pending labels, never treating an unreviewed query as no-answer."""
    questions = _questions(questions)
    if not view.snapshot_ids:
        raise ValueError("annotation corpus must contain snapshots")
    identity = view.retrieval_identity()
    return {
        "kind": "hierarchy-labels-v1",
        "snapshot_ids": identity["snapshot_ids"],
        "corpus_sha256": identity["corpus_sha256"],
        "rows": [{"query": row["query"], "tags": row.get("tags", []),
                  "answerable": None, "relevant_ids": [], "reviewed": False, "note": ""}
                 for row in questions],
    }
