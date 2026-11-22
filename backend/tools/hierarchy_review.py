"""Compare human annotations without using retrieval results as ground truth."""

from copy import deepcopy

from backend.tools.hierarchy_labels import draft_annotations, validate_annotations


def compare_annotations(left, right, view):
    before = validate_annotations(left, view)["rows"]
    after = validate_annotations(right, view)["rows"]
    indexed = {row["query"]: row for row in after}
    if ({row["query"]: set(row["tags"]) for row in before}
            != {row["query"]: set(row["tags"]) for row in after}):
        raise ValueError("annotation comparison requires identical queries and tags")
    notes = [{row["query"]: row["note"] for row in package["rows"]} for package in (left, right)]
    details = []
    for row in before:
        other = indexed[row["query"]]
        a, b = set(row["relevant_ids"]), set(other["relevant_ids"])
        details.append({"query": row["query"], "tags": sorted(set(row["tags"])),
                        "left": {"answerable": row["answerable"], "relevant_ids": sorted(a), "note": notes[0][row["query"]]},
                        "right": {"answerable": other["answerable"], "relevant_ids": sorted(b), "note": notes[1][row["query"]]},
                        "answerable_agrees": row["answerable"] == other["answerable"],
                        "exact_agreement": a == b and row["answerable"] == other["answerable"],
                        "relevance_jaccard": len(a & b) / len(a | b) if a | b else None})
    overlap = [item["relevance_jaccard"] for item in details if item["relevance_jaccard"] is not None]
    return {"kind": "hierarchy-agreement-v1", "snapshot_ids": list(view.snapshot_ids),
            "corpus_sha256": left["corpus_sha256"], "queries": len(details),
            "exact_agreements": sum(item["exact_agreement"] for item in details),
            "answerable_agreements": sum(item["answerable_agrees"] for item in details),
            "jaccard_queries": len(overlap), "mean_relevance_jaccard": sum(overlap) / len(overlap) if overlap else None,
            "disputed_queries": [item["query"] for item in details if not item["exact_agreement"]], "details": details}


def consensus_annotations(left, right, view):
    """Retain exact agreements; leave every disagreement explicitly unreviewed."""
    report = compare_annotations(left, right, view)
    result = draft_annotations([{"query": item["query"], "tags": item["tags"]} for item in report["details"]], view)
    for row, item in zip(result["rows"], report["details"]):
        row["note"] = "A: " + item["left"]["note"] + "\nB: " + item["right"]["note"]
        if item["exact_agreement"]:
            row.update(answerable=item["left"]["answerable"],
                       relevant_ids=deepcopy(item["left"]["relevant_ids"]), reviewed=True)
    validate_annotations(result, view, require_complete=False)
    return result
