"""Analyze persisted advisory observations, with explicit labels for training."""
from collections import Counter
import json
import math
import re
from pathlib import Path

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
    if (observation.get("stage") != "initial_plan" or observation.get("candidate_scope") !=
            "registered_workers_not_capability_filtered"):
        raise ValueError("unsupported observation stage or candidate scope")
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


def _index_runs(records):
    if not isinstance(records, list) or not records:
        raise ValueError("non-empty run record array required")
    indexed = {}
    for record in records:
        run_id = record.get("run_id") if isinstance(record, dict) else None
        if not isinstance(run_id, str) or not run_id.strip() or run_id in indexed:
            raise ValueError("unique run IDs required")
        indexed[run_id] = record
    return indexed


def summarize(records):
    models = {}
    disagreements = {}
    missing = skipped = 0
    for record in _index_runs(records).values():
        observation = record.get("policy_observation")
        if observation is None or observation == {}:
            missing += 1
            continue
        if isinstance(observation, dict) and observation.get("status") == "skipped":
            skipped += 1
            continue
        rows = observation_tasks(record)
        digest = observation["model_sha256"]
        bucket = models.setdefault(digest, {"runs": 0, "tasks": 0, "matched": 0})
        bucket["runs"] += 1
        bucket["tasks"] += len(rows)
        bucket["matched"] += sum(row["matched"] for row in rows)
        disagreements.setdefault(digest, Counter()).update(
            (row["agent"], row["suggested_agent"]) for row in rows if not row["matched"]
        )
    for digest, bucket in models.items():
        bucket["agreement"] = bucket["matched"] / bucket["tasks"]
        bucket["disagreements"] = [
            {"logged_agent": logged, "suggested_agent": suggested, "tasks": count}
            for (logged, suggested), count in sorted(disagreements[digest].items())
        ]
    return {"runs": len(records), "unavailable_runs": missing + skipped,
            "missing_runs": missing, "skipped_runs": skipped, "models": models,
            "warning": "Initial-plan agreement only; registered workers are not capability filtered. "
                       "This does not measure execution success or policy improvement."}


def _executed_tasks(record, observed):
    if not record.get("ended_at"):
        raise ValueError("finished run required")
    outcomes = record.get("tasks")
    if not isinstance(outcomes, list):
        raise ValueError("recorded task outcomes required")
    indexed = {}
    for task in outcomes:
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            raise ValueError("invalid recorded task")
        if task["task_id"] in indexed:
            raise ValueError("ambiguous repeated task outcome")
        indexed[task["task_id"]] = task
    return {task_id: task for task_id, task in indexed.items()
            if task_id in observed and task.get("status") in ("done", "failed")
            and task.get("agent") == observed[task_id]["agent"]}


def score_template(record):
    """Return unfilled labels for initial tasks with matching terminal outcomes."""
    observed = {row["task_id"]: row for row in observation_tasks(record)}
    executed = _executed_tasks(record, observed)
    return {task_id: None for task_id in observed if task_id in executed}


def training_rows(record, scores):
    """Join initial-plan context with terminal task costs and explicit scores."""
    observed = {row["task_id"]: row for row in observation_tasks(record)}
    executed = _executed_tasks(record, observed)
    if not isinstance(scores, dict) or not scores:
        raise ValueError("non-empty explicit score mapping required")
    rows = []
    if set(scores) - set(observed):
        raise ValueError("unknown observed task score")
    for task_id in observed:
        if task_id not in scores:
            continue
        score = scores[task_id]
        if task_id not in executed:
            raise ValueError("score must reference a terminal outcome matching the observed agent")
        context, task = observed[task_id], executed[task_id]
        rows.append({"run_id": record["run_id"], "task_id": task_id,
                     "agent": task["agent"], "attempts": task.get("attempts"),
                     "duration": task.get("duration"), "score": score,
                     **{key: context[key] for key in ("depth", "upstream", "budget_fraction", "candidates")},
                     "observation_model_sha256": record["policy_observation"]["model_sha256"]})
    from backend.tools.policy_samples import samples
    samples(rows)
    return rows


def read_history(path):
    """Read a JSON input or the immediate run JSON files in an archive directory."""
    def read_file(source):
        try:
            return json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read {source}: {exc}") from exc

    if not path.is_dir():
        return read_file(path)
    records = []
    for source in sorted(path.glob("*.json")):
        if source.is_file():
            record = read_file(source)
            if not isinstance(record, dict):
                raise ValueError(f"{source}: expected one run record")
            records.append(record)
    return records


def batch_training_rows(records, scores):
    """Export the explicitly scored subset in chronological run order."""
    from backend.tools.policy_trace import chronological_runs

    indexed = _index_runs(records)
    if not isinstance(scores, dict) or not scores or set(scores) - set(indexed):
        raise ValueError("scores must identify runs present in the history")
    ordered = chronological_runs(indexed[run_id] for run_id in scores)
    rows = []
    for record in ordered:
        run_id = record["run_id"]
        try:
            rows.extend(training_rows(record, scores[run_id]))
        except ValueError as exc:
            raise ValueError(f"run {run_id}: {exc}") from exc
    return rows


def read_scores(path):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate score key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read scores from {path}: {exc}") from exc


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Saved run JSON array or directory of run JSON files")
    parser.add_argument("--output", type=Path, help="Write JSON result to a new file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--score-template", action="store_true", help="List scoreable tasks from one run")
    mode.add_argument("--scores", type=Path,
                        help="Task-ID to score object for one run, nested by run ID for a batch")
    args = parser.parse_args()
    try:
        source = read_history(args.input)
        if args.score_template:
            result = score_template(source)
        elif args.scores:
            scores = read_scores(args.scores)
            exporter = batch_training_rows if isinstance(source, list) else training_rows
            result = exporter(source, scores)
        else:
            result = summarize(source)
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        else:
            print(payload)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
