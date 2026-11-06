"""Frozen-model descriptive evaluation; no updates or causal reward claims."""
from backend.tools.policy_samples import samples


def validate_split(training, evaluation):
    samples(training)
    samples(evaluation)
    def runs(rows):
        if any(not isinstance(row.get("run_id"), str) or not row["run_id"].strip() for row in rows):
            raise ValueError("run provenance required for split validation")
        return {row["run_id"] for row in rows}
    if runs(training) & runs(evaluation):
        raise ValueError("training and evaluation must use disjoint runs")


def evaluate(model, rows):
    from copy import deepcopy
    import hashlib
    import json
    from backend.agents.policy_store import snapshot
    def fingerprint(value):
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    model_hash = fingerprint(snapshot(model))
    model = deepcopy(model)
    observations = samples(rows)
    details = []
    for agent, candidates, x, reward in observations:
        suggested = model.recommend(candidates, x)
        details.append({"logged_agent": agent, "suggested_agent": suggested,
                        "matched": agent == suggested, "observed_reward": reward})
    return {"observations": len(details),
            "model_sha256": model_hash, "samples_sha256": fingerprint(rows),
            "agreement": sum(row["matched"] for row in details) / len(details),
            "warning": "Descriptive agreement only; no counterfactual reward estimate.",
            "details": details}


def main():
    import argparse
    import json
    from pathlib import Path
    from backend.agents.policy_store import load
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("input", type=Path)
    parser.add_argument("--training", type=Path, help="Optional source-overlap check against training rows")
    args = parser.parse_args()
    try:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
        if args.training:
            validate_split(json.loads(args.training.read_text(encoding="utf-8")), rows)
        result = evaluate(load(args.model), rows)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
