"""Build advisory comparisons without changing tasks or training the model."""
from copy import deepcopy
import hashlib
import json
import math
import asyncio
import logging
import os

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
    return {"version": 1, "advisory_only": True, "model_sha256": digest,
            "candidate_scope": "registered_workers_not_capability_filtered",
            "stage": "initial_plan", "tasks": rows}


async def observe_runtime(tasks, budget):
    """Opt-in telemetry: failures must never affect execution or approvals."""
    if os.environ.get("AGENT_POLICY_OBSERVE", "0") != "1":
        return {}
    try:
        path = os.environ.get("AGENT_POLICY_MODEL", "")
        if not path:
            raise ValueError("AGENT_POLICY_MODEL required")
        from backend.agents import orchestrator as orch
        from backend.agents.policy_store import load

        dependencies = {task["id"]: orch._task_deps(task, tasks) for task in tasks}
        candidates = sorted(orch._get_worker_registry())
        remaining = max(0., min(1., (budget.max_steps - budget.steps) / budget.max_steps)) if budget.max_steps > 0 else 0.
        return await asyncio.to_thread(
            lambda: observe_plan(load(path), tasks, dependencies, candidates, remaining)
        )
    except Exception as exc:
        logging.getLogger("agents.policy").warning("Policy observation skipped: %s", type(exc).__name__)
        return {"advisory_only": True, "status": "skipped", "reason": type(exc).__name__}


def record_observation(run_id, observation):
    """Attach to the existing run trace; repeated calls replace, never append."""
    if not observation:
        return
    try:
        from backend.observability import current_trace
        trace = current_trace()
        if trace is not None and trace.run_id == run_id:
            trace.note(policy_observation=deepcopy(observation))
    except Exception as exc:
        logging.getLogger("agents.policy").debug("Policy trace skipped: %s", type(exc).__name__)
