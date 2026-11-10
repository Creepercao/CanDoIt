"""Split labeled policy samples by run time without fitting a model."""
from datetime import datetime

from backend.tools.policy_samples import samples


def _timestamp(value, field):
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp with a timezone") from exc
    if timestamp.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return timestamp


def split_by_run_time(rows, cutoff):
    """Keep runs wholly before or on/after the cutoff, excluding overlaps."""
    boundary = _timestamp(cutoff, "cutoff")
    samples(rows)
    groups = {}
    for row in rows:
        run_id = row.get("run_id")
        if run_id is None:
            raise ValueError("run provenance required for temporal split")
        started = _timestamp(row.get("run_started_at"), f"run {run_id} start time")
        ended = _timestamp(row.get("run_ended_at"), f"run {run_id} end time")
        if ended < started:
            raise ValueError(f"run {run_id}: end time is before start time")
        timestamps, run_rows = groups.setdefault(run_id, ((started, ended), []))
        if timestamps != (started, ended):
            raise ValueError(f"run {run_id}: inconsistent timestamps across samples")
        run_rows.append(row)

    training, evaluation, excluded_run_ids = [], [], []
    for run_id, ((started, ended), run_rows) in sorted(groups.items(), key=lambda item: (item[1][0][0], item[0])):
        if ended < boundary:
            training.extend(run_rows)
        elif started >= boundary:
            evaluation.extend(run_rows)
        else:
            excluded_run_ids.append(run_id)
    return {"training": training, "evaluation": evaluation, "excluded_run_ids": excluded_run_ids}
