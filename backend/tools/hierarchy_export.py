"""Export local hierarchical lexical rankings for the retrieval evaluator."""

import argparse
import json
from pathlib import Path
import sqlite3

from backend.hierarchy import HierarchyStore
from backend.tools.retrieval_export import validate_export
from backend.tools.retrieval_io import read_json_input


def export_rankings(labels, store, depth=20, section_k=5):
    rows = validate_export(labels, depth=depth, threshold=0)
    if type(section_k) is not int or not 1 <= section_k <= 200:
        raise ValueError("section_k must be an integer between 1 and 200")
    active = sorted(item["id"] for item in store.list_versions() if item["active"])
    if not active:
        raise ValueError("store has no active snapshots")
    for row in rows:
        row["ranked_ids"] = [hit["id"] for hit in store.search(row["query"], depth, section_k)]
    if active != sorted(item["id"] for item in store.list_versions() if item["active"]):
        raise ValueError("active snapshots changed during export")
    return rows, active


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--section-k", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("output already exists")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        if not args.store.is_file():
            raise ValueError("store does not exist")
        labels, source = read_json_input(args.labels)
        rows, active = export_rankings(labels, HierarchyStore(args.store), args.depth, args.section_k)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({"queries": len(rows), "active_snapshot_ids": active, "input": source,
                      "configuration": {"depth": args.depth, "section_k": args.section_k,
                                        "retriever": "hierarchical-bm25-rrf-v1"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
