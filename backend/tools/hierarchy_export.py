"""Export local hierarchical lexical rankings for the retrieval evaluator."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from backend.hierarchy import HierarchyStore
from backend.tools.retrieval_export import validate_export
from backend.tools.retrieval_io import read_json_input


def export_report(labels, store, depth=20, section_k=5, sources=None, *, route="rrf"):
    rows = validate_export(labels, depth=depth, threshold=0)
    if type(section_k) is not int or not 1 <= section_k <= 200:
        raise ValueError("section_k must be an integer between 1 and 200")
    view = store.prepare(sources) if sources is not None else store.prepare()
    active = list(view.snapshot_ids)
    if not active:
        raise ValueError("store has no active snapshots")
    identity = view.retrieval_identity(depth, section_k, route=route)
    for row in rows:
        row["ranked_ids"] = [hit["id"] for hit in view.search(row["query"], depth, section_k, route=route)]
    return {"rows": rows, **identity}


def export_rankings(labels, store, depth=20, section_k=5, sources=None, *, route="rrf"):
    report = export_report(labels, store, depth, section_k, sources, route=route)
    return report["rows"], report["snapshot_ids"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--section-k", type=int, default=5)
    parser.add_argument("--route", choices=("rrf", "body"), default="rrf")
    parser.add_argument("--source", dest="sources", action="append", help="Repeat to select active source keys")
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("output already exists")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        if not args.store.is_file():
            raise ValueError("store does not exist")
        labels, source = read_json_input(args.labels)
        report = export_report(labels, HierarchyStore(args.store), args.depth, args.section_k, args.sources, route=args.route)
        rows = report.pop("rows")
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        rankings_sha256 = hashlib.sha256(args.output.read_bytes()).hexdigest()
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({**report, "queries": len(rows), "active_snapshot_ids": report["snapshot_ids"], "input": source,
                      "sources": args.sources, "rankings_sha256": rankings_sha256},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
