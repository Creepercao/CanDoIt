"""Export labeled queries through the production knowledge-base retriever.

Usage: python -m backend.tools.retrieval_export labels.json rankings.json
Labels use query/relevant_ids; output can be scored by retrieval_eval.
"""

import argparse
import asyncio
import json
import math
from pathlib import Path

from backend.tools.retrieval_eval import evaluate
from backend.tools.retrieval_io import read_json_input


def validate_export(labels, depth=20, threshold=0.3):
    if type(depth) is not int or depth < 1 or depth > 200:
        raise ValueError("depth must be between 1 and 200")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("threshold must be finite and between -1 and 1")
    if not isinstance(labels, list) or any(not isinstance(row, dict) for row in labels):
        raise ValueError("labels must be an array of objects")
    rows = [{"query": row.get("query"), "relevant_ids": row.get("relevant_ids"),
             "ranked_ids": [],
             **({key: row[key] for key in ("answerable", "tags") if key in row})}
            for row in labels]
    evaluate(rows)  # Validate the entire corpus before making any API calls.
    if len({row["query"] for row in rows}) != len(rows):
        raise ValueError("export requires unique queries for later comparison")
    return rows


async def export_rankings(labels, search, depth=20, threshold=0.3):
    rows = validate_export(labels, depth, threshold)
    for row in rows:
        results = await search(row["query"], top_k=depth, threshold=threshold)
        row["ranked_ids"] = [entry["id"] for entry in results]
    evaluate(rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--depth", type=int, default=20)
    parser.add_argument("--threshold", type=float, required=True,
                        help="Use the threshold of the retrieval configuration being evaluated")
    args = parser.parse_args()

    async def run():
        from backend.research_cache import get_knowledge_base

        kb = get_knowledge_base()
        if kb is None:
            raise ValueError("knowledge base is disabled")
        await kb._ensure_init()
        if not kb.available:
            raise ValueError("knowledge base is unavailable")
        async def strict_search(query, **kwargs):
            return await kb.search(query, strict=True, **kwargs)

        return await export_rankings(labels, strict_search, args.depth, args.threshold)

    try:
        labels, _ = read_json_input(args.labels)
        validate_export(labels, args.depth, args.threshold)
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError(f"output already exists: {args.output}")
        if not args.output.parent.is_dir():
            raise FileNotFoundError(f"output directory does not exist: {args.output.parent}")
        rows = asyncio.run(run())
        # Exclusive creation protects previous experimental results.
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
