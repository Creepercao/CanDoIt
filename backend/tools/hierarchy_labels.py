"""Human relevance annotations bound to an immutable hierarchy corpus."""

from copy import deepcopy

from backend.tools.retrieval_export import validate_export


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


def validate_annotations(package, view, *, require_complete=True):
    """Validate provenance and IDs before accepting human relevance decisions."""
    if type(require_complete) is not bool:
        raise ValueError("require_complete must be boolean")
    if not isinstance(package, dict) or set(package) != {"kind", "snapshot_ids", "corpus_sha256", "rows"}:
        raise ValueError("invalid annotation package fields")
    if package["kind"] != "hierarchy-labels-v1":
        raise ValueError("unsupported annotation kind")
    identity = view.retrieval_identity()
    if (not view.snapshot_ids or package["snapshot_ids"] != identity["snapshot_ids"]
            or package["corpus_sha256"] != identity["corpus_sha256"]):
        raise ValueError("annotation corpus does not match selected snapshots")
    rows = package["rows"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("annotation rows must be an array of objects")
    _questions([{"query": row.get("query"), "tags": row.get("tags", [])} for row in rows])
    completed, pending = [], []
    for row in rows:
        if set(row) != {"query", "tags", "answerable", "relevant_ids", "reviewed", "note"}:
            raise ValueError("invalid annotation row fields")
        if type(row["reviewed"]) is not bool or not isinstance(row["note"], str):
            raise ValueError("reviewed must be boolean and note must be text")
        ids = row["relevant_ids"]
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() for item in ids):
            raise ValueError("relevant_ids must be a list of non-empty string IDs")
        if len(ids) != len(set(ids)):
            raise ValueError("relevant_ids must not contain duplicates")
        answerable = row["answerable"]
        if answerable is not None and type(answerable) is not bool:
            raise ValueError("answerable must be boolean or null while pending")
        if row["reviewed"]:
            if answerable is None:
                raise ValueError("reviewed annotations need an explicit answerable decision")
            completed.append({key: deepcopy(row[key]) for key in ("query", "tags", "answerable", "relevant_ids")})
        else:
            pending.append(row["query"])
    view.require_chunk_ids([item for row in rows for item in row["relevant_ids"]])
    if completed:
        validate_export(completed)  # Reuse the evaluator's answerable/relevance contract.
    if require_complete and pending:
        raise ValueError(f"annotations still pending review: {len(pending)}")
    return {"rows": completed, "queries": len(rows), "reviewed_queries": len(completed),
            "pending_queries": pending, "complete": not pending,
            "snapshot_ids": identity["snapshot_ids"], "corpus_sha256": identity["corpus_sha256"]}
