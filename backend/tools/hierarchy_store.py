"""Manage an explicitly selected offline hierarchical snapshot database."""

import argparse
import json
from pathlib import Path
import sqlite3

from backend.hierarchy import HierarchyStore
from backend.tools.retrieval_io import read_json_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Also save JSON; path must not exist")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("import")
    load.add_argument("snapshot", type=Path)
    load.add_argument("--activate", action="store_true")
    versions = commands.add_parser("list")
    versions.add_argument("--source")
    commands.add_parser("verify")
    activate = commands.add_parser("activate")
    activate.add_argument("snapshot_id")
    context = commands.add_parser("context")
    context.add_argument("chunk_ids", type=Path, help="UTF-8 JSON array of ranked chunk IDs")
    context.add_argument("--max-chars", type=int, default=4000)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--section-k", type=int, default=5)
    search.add_argument("--with-context", action="store_true")
    search.add_argument("--max-chars", type=int, default=4000)
    args = parser.parse_args()
    try:
        if args.output and (args.output.exists() or args.output.is_symlink()):
            raise FileExistsError("output already exists")
        if args.output and not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        store = HierarchyStore(args.store)
        if args.command != "import" and not args.store.is_file():
            raise ValueError("store does not exist; import a snapshot first")
        if args.command == "import":
            snapshot, source = read_json_input(args.snapshot)
            snapshot_id = store.save(snapshot)
            if args.activate:
                store.activate(snapshot_id)
            result = {"snapshot_id": snapshot_id, "activated": args.activate, "input": source}
        elif args.command == "list":
            result = {"versions": store.list_versions(args.source)}
        elif args.command == "activate":
            result = {"snapshot_id": args.snapshot_id, "source": store.activate(args.snapshot_id)}
        elif args.command == "verify":
            result = store.verify()
        elif args.command == "search":
            if args.max_chars < 1:
                raise ValueError("max_chars must be positive")
            hits = store.search(args.query, args.top_k, args.section_k)
            result = {"query": args.query, "hits": hits,
                      "configuration": {"top_k": args.top_k, "section_k": args.section_k}}
            if args.with_context:
                result["context"] = store.context([hit["id"] for hit in hits], args.max_chars)
        else:
            chunk_ids, source = read_json_input(args.chunk_ids)
            result = {**store.context(chunk_ids, args.max_chars), "input": source}
        report = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(report + "\n")
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(report)


if __name__ == "__main__":
    main()
