"""Join recorded task costs with explicit human annotations, never infer credit."""
from backend.tools.policy_samples import samples


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
