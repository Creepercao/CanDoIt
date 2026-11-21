"""Export regressed queries with both rankings for manual review, without models."""

import argparse
import json
from pathlib import Path

from backend.tools.hierarchy_artifact import decode_rankings, require_comparable_experiments
from backend.tools.retrieval_eval import compare
from backend.tools.retrieval_io import read_json_input


def build_review(comparison, metric=None, tag=None):
    summaries = comparison["change_summary"]
    if metric is not None and metric not in summaries:
        raise ValueError("review metric must name a higher-is-better comparison metric")
    if tag is not None and tag not in comparison["slices"]:
        raise ValueError("review slice must name an existing business tag")
    metrics = [metric] if metric is not None else list(summaries)
    before = {item["query"]: item for item in comparison["baseline"]["details"]}
    after = {item["query"]: item for item in comparison["candidate"]["details"]}
    cases = []
    for change in comparison["query_changes"]:
        query = change["query"]
        regressed = [key for key in metrics if query in summaries[key]["regressed"]]
        if regressed and (tag is None or tag in before[query]["tags"]):
            cases.append({"query": query, "regressed_metrics": regressed,
                          "baseline": before[query], "candidate": after[query],
                          "changes": change, "review_note": ""})
    return {"k": comparison["candidate"]["k"], "metrics": metrics, "slice": tag,
            "selected_queries": len(cases), "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--metric")
    parser.add_argument("--slice", dest="tag")
    args = parser.parse_args()
    try:
        value, candidate_input = read_json_input(args.candidate)
        candidate, candidate_experiment = decode_rankings(value, args.k)
        value, baseline_input = read_json_input(args.baseline)
        baseline, baseline_experiment = decode_rankings(value, args.k)
        require_comparable_experiments(baseline_experiment, candidate_experiment)
        result = build_review(compare(baseline, candidate, args.k), args.metric, args.tag)
        result["inputs"] = {"candidate": candidate_input, "baseline": baseline_input}
        if candidate_experiment is not None:
            result["experiments"] = {"candidate": candidate_experiment, "baseline": baseline_experiment}
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
