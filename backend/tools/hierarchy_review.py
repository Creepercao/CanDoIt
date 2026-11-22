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


def annotation_coverage(package, view):
    """Count reviewed evidence locations, without claiming semantic coverage."""
    checked = validate_annotations(package, view, require_complete=False)
    inventory = view.inventory()
    chunks = {item["id"]: item for item in inventory["chunks"]}
    selected = {identifier for row in checked["rows"] for identifier in row["relevant_ids"]}

    def counts(scope):
        ids = {item["id"] for item in scope}
        return {"chunks": len(ids), "labeled_chunks": len(ids & selected),
                "evidence_queries": sum(bool(ids.intersection(row["relevant_ids"])) for row in checked["rows"])}

    return {"kind": "hierarchy-coverage-v1", "snapshot_ids": list(view.snapshot_ids),
            "corpus_sha256": package["corpus_sha256"], "queries": checked["queries"],
            "reviewed_queries": checked["reviewed_queries"], "pending_queries": checked["pending_queries"],
            "answerable_queries": sum(row["answerable"] for row in checked["rows"]),
            "no_answer_queries": sum(not row["answerable"] for row in checked["rows"]),
            **counts(chunks.values()),
            "sources": [{"source": document["source"], **counts(item for item in chunks.values() if item["source"] == document["source"])}
                        for document in inventory["documents"]],
            "sections": [{"id": section["id"], "source": section["source"], "path": section["path"],
                          **counts(item for item in chunks.values() if item["parent_id"] == section["id"])}
                         for section in inventory["sections"]]}


def adjudicate_annotations(package, decisions, view):
    """Resolve exactly the pending questions with explicit human decisions."""
    checked = validate_annotations(package, view, require_complete=False)
    if not isinstance(decisions, list):
        raise ValueError("decisions must be an array")
    indexed = {}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"query", "answerable", "relevant_ids", "note"}:
            raise ValueError("each decision requires query, answerable, relevant_ids and note")
        query = decision["query"]
        if not isinstance(query, str) or not query.strip() or query in indexed:
            raise ValueError("decisions require unique non-empty queries")
        if not isinstance(decision["note"], str) or not decision["note"].strip():
            raise ValueError("each decision requires a non-empty rationale note")
        indexed[query] = decision
    if set(indexed) != set(checked["pending_queries"]):
        raise ValueError("decisions must resolve exactly all pending queries")
    result = deepcopy(package)
    for row in result["rows"]:
        if row["query"] in indexed:
            decision = indexed[row["query"]]
            row.update(answerable=decision["answerable"], relevant_ids=deepcopy(decision["relevant_ids"]),
                       note=row["note"] + "\n裁决: " + decision["note"], reviewed=True)
    validate_annotations(result, view)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare", help="Compare two fully reviewed annotation files")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    consensus = commands.add_parser("consensus", help="Save agreements and leave disagreements pending")
    consensus.add_argument("left", type=Path)
    consensus.add_argument("right", type=Path)
    consensus.add_argument("--output", type=Path, required=True)
    coverage = commands.add_parser("coverage", help="Count reviewed evidence by source and section")
    coverage.add_argument("left", type=Path)
    coverage.add_argument("--output", type=Path, required=True)
    adjudicate = commands.add_parser("adjudicate", help="Resolve all pending queries using explicit decisions and reasons")
    adjudicate.add_argument("left", type=Path, help="Pending annotation package")
    adjudicate.add_argument("right", type=Path, help="JSON array of human decisions")
    adjudicate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("output already exists")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        if not args.store.is_file():
            raise ValueError("store does not exist")
        left, left_input = read_json_input(args.left)
        if not isinstance(left, dict):
            raise ValueError("left input must be an annotation package")
        view = HierarchyStore(args.store).prepare_versions(left.get("snapshot_ids"))
        if args.command == "coverage":
            report = annotation_coverage(left, view)
            report["inputs"] = {"left": left_input}
            artifact = report
        else:
            right, right_input = read_json_input(args.right)
            if args.command == "adjudicate":
                artifact = adjudicate_annotations(left, right, view)
                report = {"queries": len(artifact["rows"]), "disputed_queries": []}
            else:
                report = compare_annotations(left, right, view)
                artifact = consensus_annotations(left, right, view) if args.command == "consensus" else report
            report["inputs"] = {"left": left_input, "right": right_input}
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"output": str(args.output), "queries": report["queries"],
                          "disputed_queries": report.get("disputed_queries", []), "inputs": report["inputs"]}, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
