"""Diagnose a reviewed label set against its pinned offline lexical corpus."""

from collections import Counter

from backend.tools.hierarchy_labels import validate_annotations


STAGES = ("no_query_terms", "section_no_match", "section_pruned", "chunk_no_match", "below_top_k", "returned")


def diagnose_labels(package, view, top_k=5, section_k=5, *, route="rrf"):
    rows = validate_annotations(package, view)["rows"]
    identity = view.retrieval_identity(top_k, section_k, route=route)
    details = []
    for row in rows:
        trace = view.relevance_trace(row["query"], row["relevant_ids"], top_k, section_k, route=route)
        details.append({"query": row["query"], "tags": row["tags"], "answerable": row["answerable"],
                        "ranked_ids": [hit["id"] for hit in trace["hits"]],
                        "relevance_trace": trace["relevance_trace"], "diagnostics": trace["diagnostics"]})

    def aggregate(items):
        counts = Counter(hit["stage"] for item in items for hit in item["relevance_trace"])
        return {"queries": len(items), "answerable_queries": sum(item["answerable"] for item in items),
                "no_answer_queries": sum(not item["answerable"] for item in items),
                "no_answer_nonempty_queries": sum(not item["answerable"] and bool(item["ranked_ids"]) for item in items),
                "relevant_chunks": sum(counts.values()), "stages": {stage: counts[stage] for stage in STAGES}}

    return {"kind": "hierarchy-diagnosis-v1", **identity, **aggregate(details), "details": details,
            "slices": {tag: aggregate([item for item in details if tag in item["tags"]])
                       for tag in sorted({tag for item in details for tag in item["tags"]})}}
