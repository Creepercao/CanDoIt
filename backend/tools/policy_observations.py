"""Analyze persisted advisory observations, with explicit labels for training."""
import math
import re

from backend.agents.policy_features import features


def observation_tasks(record):
    if not isinstance(record, dict) or not isinstance(record.get("run_id"), str) or not record["run_id"].strip():
        raise ValueError("run_id required")
    observation = record.get("policy_observation")
    if not isinstance(observation, dict) or not observation or observation.get("status") == "skipped":
        raise ValueError("successful policy observation required")
    # Records produced before versioning are schema 1.
    if observation.get("version", 1) != 1 or observation.get("advisory_only") is not True:
        raise ValueError("unsupported observation schema")
    digest = observation.get("model_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("model fingerprint required")
    rows = observation.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise ValueError("observation tasks required")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid observation task")
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip() or task_id in seen:
            raise ValueError("unique observed task IDs required")
        seen.add(task_id)
        candidates, scores = row.get("candidates"), row.get("scores")
        if (not isinstance(candidates, list) or not candidates or
            any(not isinstance(c, str) or not c.strip() for c in candidates) or
            len(set(candidates)) != len(candidates) or not isinstance(scores, dict) or
            set(scores) != set(candidates) or row.get("agent") not in candidates):
            raise ValueError("invalid observed candidates")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in scores.values()):
            raise ValueError("invalid observed scores")
        suggestion = max(sorted(scores), key=scores.get)
        if row.get("suggested_agent") != suggestion or row.get("matched") is not (suggestion == row["agent"]):
            raise ValueError("inconsistent observed suggestion")
        try:
            expected = features(row["depth"], row["upstream"], row["budget_fraction"])
            if row.get("features") != expected:
                raise ValueError("inconsistent observed features")
        except KeyError as exc:
            raise ValueError("observed context required") from exc
    return rows


def summarize(records):
    if not isinstance(records, list) or not records:
        raise ValueError("non-empty run record array required")
    seen, models = set(), {}
    unavailable = 0
    for record in records:
        run_id = record.get("run_id") if isinstance(record, dict) else None
        if not isinstance(run_id, str) or not run_id.strip() or run_id in seen:
            raise ValueError("unique run IDs required")
        seen.add(run_id)
        observation = record.get("policy_observation")
        if not observation or (isinstance(observation, dict) and observation.get("status") == "skipped"):
            unavailable += 1
            continue
        rows = observation_tasks(record)
        bucket = models.setdefault(observation["model_sha256"], {"runs": 0, "tasks": 0, "matched": 0})
        bucket["runs"] += 1
        bucket["tasks"] += len(rows)
        bucket["matched"] += sum(row["matched"] for row in rows)
    for bucket in models.values():
        bucket["agreement"] = bucket["matched"] / bucket["tasks"]
    return {"runs": len(records), "unavailable_runs": unavailable, "models": models,
            "warning": "Initial-plan agreement only; registered workers are not capability filtered. "
                       "This does not measure execution success or policy improvement."}
