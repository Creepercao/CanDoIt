"""Join recorded task costs with explicit human annotations, never infer credit."""
from backend.tools.policy_samples import samples


def chronological_runs(records):
    """Order runs by start timestamp, with run ID as a deterministic tiebreak."""
    from datetime import datetime
    ordered = []
    seen = set()
    for record in records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip() or run_id in seen:
            raise ValueError("unique run IDs required")
        seen.add(run_id)
        try:
            started = datetime.fromisoformat(record["started_at"])
            if started.tzinfo is None:
                raise ValueError("timezone required")
        except (KeyError, TypeError) as exc:
            raise ValueError("valid started_at required") from exc
        ordered.append((started, run_id, record))
    return [record for _, _, record in sorted(ordered, key=lambda item: (item[0], item[1]))]


def convert_runs(records, annotations):
    """Order runs by start timestamp; retain recorded task order within a run."""
    ordered = chronological_runs(records)
    if set(annotations) != {record["run_id"] for record in ordered}:
        raise ValueError("annotations must match run IDs")
    rows = []
    for record in ordered:
        run_id = record["run_id"]
        # Annotation object ordering must not reorder the recorded tasks.
        labels = annotations[run_id]
        ordered_labels = {task["task_id"]: labels[task["task_id"]]
                          for task in record["tasks"] if task["task_id"] in labels}
        if set(ordered_labels) != set(labels):
            raise ValueError("unknown annotated task")
        rows.extend(convert(record, ordered_labels))
    samples(rows)
    return rows


def convert(record, annotations):
    if not isinstance(record, dict) or not record.get("run_id"):
        raise ValueError("run_id required")
    tasks = record.get("tasks")
    if not isinstance(tasks, list) or not isinstance(annotations, dict):
        raise ValueError("tasks array and annotation mapping required")
    indexed = {}
    for task in tasks:
        if not isinstance(task, dict) or not task.get("task_id") or task["task_id"] in indexed:
            raise ValueError("unique non-empty task IDs required")
        indexed[task["task_id"]] = task
    rows = []
    for task_id, label in annotations.items():
        if task_id not in indexed or not isinstance(label, dict):
            raise ValueError("annotation must reference a recorded task")
        task = indexed[task_id]
        row = {**label, "agent": task.get("agent"), "attempts": task.get("attempts"),
               "duration": task.get("duration"), "run_id": record["run_id"], "task_id": task_id}
        rows.append(row)
    samples(rows)
    return rows


def annotation_template(record):
    tasks = record.get("tasks", [])
    result = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not task_id or task_id in result:
            raise ValueError("unique task IDs required")
        result[task_id] = {"candidates": [], "score": None}
    return result


def main():
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--batch", action="store_true", help="Read an array of runs and run-keyed annotations")
    parser.add_argument("--annotations", type=Path,
                        help="Omit to generate an unfilled annotation template")
    args = parser.parse_args()
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        if args.batch:
            if not isinstance(record, list) or not record or any(not isinstance(r, dict) for r in record):
                raise ValueError("batch input must be a non-empty run array")
            if not args.annotations:
                raise ValueError("batch conversion requires annotations")
            result = convert_runs(record, json.loads(args.annotations.read_text(encoding="utf-8")))
        elif not isinstance(record, dict):
            raise ValueError("record must be an object")
        else:
            result = (convert(record, json.loads(args.annotations.read_text(encoding="utf-8")))
                      if args.annotations else annotation_template(record))
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(payload)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
