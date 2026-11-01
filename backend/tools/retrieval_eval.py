"""Score saved retrieval rankings without model or database dependencies."""

import argparse
import json
from pathlib import Path


def evaluate(rows: list[dict], k: int = 5) -> dict:
    """Macro recall@k and MRR over the complete supplied rankings.

    Each row contains query, relevant_ids and ranked_ids (best first).
    Duplicate retrieved IDs count once and do not consume extra ranks.
    """
    if k < 1 or not rows:
        raise ValueError("k must be positive and evaluation rows must not be empty")
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
                        "reciprocal_rank": reciprocal_rank})
    return {"queries": len(details), "k": k,
            f"recall@{k}": sum(item["recall"] for item in details) / len(details),
            "mrr": sum(item["reciprocal_rank"] for item in details) / len(details),
            "details": details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTF-8 JSON array of labeled rankings")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    try:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("input must be an array of objects")
        result = evaluate(rows, args.k)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
