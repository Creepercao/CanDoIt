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


def main():
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Labeled samples with run timestamps")
    parser.add_argument("--cutoff", required=True, help="ISO 8601 timestamp with timezone")
    parser.add_argument("--partition", choices=("summary", "training", "evaluation"), default="summary")
    parser.add_argument("--output", type=Path, help="Write JSON to a new file instead of stdout")
    args = parser.parse_args()
    try:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
        split = split_by_run_time(rows, args.cutoff)
        if args.partition == "summary":
            result = {
                "cutoff": args.cutoff,
                "training_rows": len(split["training"]),
                "evaluation_rows": len(split["evaluation"]),
                "excluded_run_ids": split["excluded_run_ids"],
            }
        else:
            if not split["training"] or not split["evaluation"]:
                raise ValueError("choose a cutoff leaving samples in both partitions")
            result = split[args.partition]
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
