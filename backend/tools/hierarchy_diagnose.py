"""Diagnose a reviewed label set against its pinned offline lexical corpus."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_labels import validate_annotations
from backend.tools.retrieval_io import read_json_input


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--section-k", type=int, default=5)
    parser.add_argument("--route", choices=("rrf", "body"), default="rrf")
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("output already exists")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        if not args.store.is_file():
            raise ValueError("store does not exist")
        package, source = read_json_input(args.labels)
        if not isinstance(package, dict):
            raise ValueError("labels must be a bound annotation package")
        view = HierarchyStore(args.store).prepare_versions(package.get("snapshot_ids"))
        report = diagnose_labels(package, view, args.top_k, args.section_k, route=args.route)
        report["input"] = source
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"output": str(args.output), "queries": report["queries"], "stages": report["stages"]}, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
