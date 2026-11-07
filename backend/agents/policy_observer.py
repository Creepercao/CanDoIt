"""Build advisory comparisons without changing tasks or training the model."""
from copy import deepcopy
import hashlib
import json
import math

from backend.agents.policy_context import plan_contexts
from backend.agents.policy_store import snapshot


def observe_plan(model, tasks, dependencies, candidates, budget_fraction):
    if not isinstance(candidates, list) or not candidates or any(
        not isinstance(name, str) or not name.strip() for name in candidates
    ):
        raise ValueError("candidate agents required")
    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids) or set(ids) != set(dependencies):
        raise ValueError("unique task IDs matching dependency graph required")
    contexts = plan_contexts(dependencies, budget_fraction)
    digest = hashlib.sha256(json.dumps(snapshot(model), sort_keys=True,
                            allow_nan=False).encode("utf-8")).hexdigest()
    model = deepcopy(model)
    rows = []
    for task in tasks:
        if task["agent"] not in candidates:
            raise ValueError("planned agent is not registered")
        context = contexts[task["id"]]
        scores = {name: model.score(name, context["features"]) for name in sorted(set(candidates))}
        if any(not math.isfinite(value) for value in scores.values()):
            raise ValueError("non-finite policy score")
        suggestion = max(scores, key=scores.get)
        rows.append({"task_id": task["id"], "agent": task["agent"],
                     "suggested_agent": suggestion, "matched": suggestion == task["agent"],
                     "candidates": sorted(scores), "scores": scores, **context})
    return {"advisory_only": True, "model_sha256": digest,
            "candidate_scope": "registered_workers_not_capability_filtered",
            "stage": "initial_plan", "tasks": rows}
