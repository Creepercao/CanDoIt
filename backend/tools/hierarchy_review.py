"""Compare human annotations without using retrieval results as ground truth."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_labels import draft_annotations, validate_annotations
from backend.tools.retrieval_io import read_json_input


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare", help="Compare two fully reviewed annotation files")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("output already exists")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        if not args.store.is_file():
            raise ValueError("store does not exist")
        left, left_input = read_json_input(args.left)
        right, right_input = read_json_input(args.right)
        if not isinstance(left, dict):
            raise ValueError("left input must be an annotation package")
        view = HierarchyStore(args.store).prepare_versions(left.get("snapshot_ids"))
        report = compare_annotations(left, right, view)
        report["inputs"] = {"left": left_input, "right": right_input}
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"output": str(args.output), "queries": report["queries"],
                          "disputed_queries": report["disputed_queries"]}, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
