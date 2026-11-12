"""Score saved retrieval rankings without model or database dependencies."""

import argparse
import json
from pathlib import Path


def metric_names(k):
    return (f"recall@{k}", f"hit_rate@{k}", f"mrr@{k}", "mrr",
            "no_answer_empty_rate", "no_answer_retrieval_rate")


def _aggregate(details, k):
    supported = [item for item in details if item["answerable"]]
    unsupported = [item for item in details if not item["answerable"]]
    empty_rate = (sum(item["retrieved_count"] == 0 for item in unsupported) / len(unsupported)
                  if unsupported else None)
    return {"queries": len(details), "k": k,
            "answerable_queries": len(supported), "no_answer_queries": len(unsupported),
            "no_answer_empty_rate": empty_rate,
            "no_answer_retrieval_rate": 1 - empty_rate if empty_rate is not None else None,
            f"hit_rate@{k}": sum(item["hit"] for item in supported) / len(supported) if supported else None,
            f"recall@{k}": sum(item["recall"] for item in supported) / len(supported) if supported else None,
            f"mrr@{k}": sum(item["reciprocal_rank_at_k"] for item in supported) / len(supported) if supported else None,
            "mrr": sum(item["reciprocal_rank"] for item in supported) / len(supported) if supported else None}


def evaluate(rows: list[dict], k: int = 5) -> dict:
    """Macro recall@k, MRR@k and MRR over the complete supplied rankings.

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
        tags = row.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise ValueError("tags must be a list of non-empty strings")
        for key in ("relevant_ids", "ranked_ids"):
            values = row.get(key)
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"{key} must be a list of non-empty string IDs")
        relevant = set(row["relevant_ids"])
        answerable = row.get("answerable", True)
        if type(answerable) is not bool or answerable != bool(relevant):
            raise ValueError("answerable queries need relevant IDs; no-answer queries need answerable=false and no relevant IDs")
        ranked = list(dict.fromkeys(row["ranked_ids"]))
        recall = len(relevant.intersection(ranked[:k])) / len(relevant) if answerable else None
        reciprocal_rank = next(
            (1 / rank for rank, item in enumerate(ranked, 1) if item in relevant), 0.0
        )
        details.append({"query": row["query"], "tags": sorted(set(tags)), "answerable": answerable, "recall": recall,
                        "hit": bool(relevant.intersection(ranked[:k])),
                        "retrieved_count": len(ranked),
                        "reciprocal_rank_at_k": reciprocal_rank if answerable and reciprocal_rank >= 1 / k else (0.0 if answerable else None),
                        "reciprocal_rank": reciprocal_rank if answerable else None})
    slices = {tag: _aggregate([item for item in details if tag in item["tags"]], k)
              for tag in sorted({tag for item in details for tag in item["tags"]})}
    return {**_aggregate(details, k), "details": details, "slices": slices}


def compare(baseline, candidate, k=5):
    """Compare identical labeled query sets, regardless of row order."""
    before, after = evaluate(baseline, k), evaluate(candidate, k)
    def labels(rows):
        result = {row["query"]: (set(row["relevant_ids"]), set(row.get("tags", []))) for row in rows}
        if len(result) != len(rows):
            raise ValueError("comparison requires unique queries")
        return result
    if labels(baseline) != labels(candidate):
        raise ValueError("comparison requires identical queries, relevance labels and tags")
    metrics = metric_names(k)
    def deltas(old, new):
        return {key: new[key] - old[key] if old[key] is not None and new[key] is not None else None
                for key in metrics}
    candidate_details = {item["query"]: item for item in after["details"]}
    query_changes = []
    for old in before["details"]:
        new = candidate_details[old["query"]]
        answerable = old["answerable"]
        query_changes.append({
            "query": old["query"], "answerable": answerable,
            "recall_delta": new["recall"] - old["recall"] if answerable else None,
            "hit_delta": int(new["hit"]) - int(old["hit"]) if answerable else None,
            "reciprocal_rank_delta": (new["reciprocal_rank"] - old["reciprocal_rank"]
                                      if answerable else None),
            "reciprocal_rank_at_k_delta": (new["reciprocal_rank_at_k"] - old["reciprocal_rank_at_k"]
                                           if answerable else None),
            "empty_retrieval_delta": (int(new["retrieved_count"] == 0) - int(old["retrieved_count"] == 0)
                                      if not answerable else None),
        })
    return {"baseline": before, "candidate": after,
            "query_changes": query_changes,
            "slices": {tag: {"baseline": before["slices"][tag], "candidate": after["slices"][tag],
                             "delta": deltas(before["slices"][tag], after["slices"][tag])}
                       for tag in before["slices"]},
            "delta": deltas(before, after)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTF-8 JSON array of labeled rankings")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--baseline", type=Path, help="Compare against saved baseline rankings")
    parser.add_argument("--output", type=Path, help="Also save the JSON report; path must not exist")
    args = parser.parse_args()
    try:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("input must be an array of objects")
        result = evaluate(rows, args.k)
        if args.baseline:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            result = compare(baseline, rows, args.k)
        report = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(report + "\n")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(report)


if __name__ == "__main__":
    main()
