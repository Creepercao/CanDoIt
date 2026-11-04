"""Print advisory agent scores from a saved policy; never executes tasks."""
import argparse
import json
from pathlib import Path

from backend.agents.policy_features import features
from backend.agents.policy_store import load


def suggest(model, candidates, depth=0, upstream=0, budget_fraction=1):
    if not isinstance(candidates, list) or not candidates or any(
        not isinstance(name, str) or not name.strip() for name in candidates
    ):
        raise ValueError("non-empty candidate agent list required")
    x = features(depth, upstream, budget_fraction)
    scores = {name: model.score(name, x) for name in sorted(set(candidates))}
    return {"advisory_only": True, "suggested_agent": max(scores, key=scores.get),
            "scores": scores}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("candidates", nargs="+")
    parser.add_argument("--depth", type=int, default=0)
    parser.add_argument("--upstream", type=int, default=0)
    parser.add_argument("--budget-fraction", type=float, default=1)
    args = parser.parse_args()
    try:
        result = suggest(load(args.model), args.candidates, args.depth,
                         args.upstream, args.budget_fraction)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
