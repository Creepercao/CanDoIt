"""Score saved retrieval rankings without model or database dependencies."""

import argparse
import json
from pathlib import Path


def evaluate(rows: list[dict], k: int = 5) -> dict:
    """Macro recall@k and MRR over the complete supplied rankings.

    Each row contains query, relevant_ids and ranked_ids (best first).
    Duplicate retrieved IDs count once and do not consume extra ranks.
    """
    if type(k) is not int or k < 1 or not rows:
        raise ValueError("k must be positive and evaluation rows must not be empty")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("input must be an array of objects")
    details = []
    for row in rows:
        if not isinstance(row.get("query"), str) or not row["query"].strip():
            raise ValueError("each row needs a non-empty query")
        for key in ("relevant_ids", "ranked_ids"):
            values = row.get(key)
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{key} must be a list of non-empty string IDs")
        relevant = set(row["relevant_ids"])
        if not relevant:
            raise ValueError("each query needs at least one relevant ID")
        ranked = list(dict.fromkeys(row["ranked_ids"]))
        recall = len(relevant.intersection(ranked[:k])) / len(relevant)
        reciprocal_rank = next(
            (1 / rank for rank, item in enumerate(ranked, 1) if item in relevant), 0.0
        )
        details.append({"query": row["query"], "recall": recall,
                        "hit": bool(relevant.intersection(ranked[:k])),
                        "reciprocal_rank": reciprocal_rank})
    return {"queries": len(details), "k": k,
            f"hit_rate@{k}": sum(item["hit"] for item in details) / len(details),
            f"recall@{k}": sum(item["recall"] for item in details) / len(details),
            "mrr": sum(item["reciprocal_rank"] for item in details) / len(details),
            "details": details}


def compare(baseline, candidate, k=5):
    """Compare identical labeled query sets, regardless of row order."""
    before, after = evaluate(baseline, k), evaluate(candidate, k)
    def labels(rows):
        result = {row["query"]: set(row["relevant_ids"]) for row in rows}
        if len(result) != len(rows):
            raise ValueError("comparison requires unique queries")
        return result
    if labels(baseline) != labels(candidate):
        raise ValueError("comparison requires identical queries and relevance labels")
    return {"baseline": before, "candidate": after,
            "delta": {key: after[key] - before[key]
                      for key in (f"recall@{k}", f"hit_rate@{k}", "mrr")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTF-8 JSON array of labeled rankings")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--baseline", type=Path, help="Compare against saved baseline rankings")
    args = parser.parse_args()
    try:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("input must be an array of objects")
        result = evaluate(rows, args.k)
        if args.baseline:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            result = compare(baseline, rows, args.k)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
