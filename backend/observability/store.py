"""Run-record storage — one JSON file per run under ``outputs/runs/``.

Deliberately file-per-run with no central index: a crashed write can only lose
its own record, and listing is a directory scan rather than a mutable shared
document.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("observability.store")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def runs_dir() -> Path:
    from backend.api import OUTPUTS_DIR

    return OUTPUTS_DIR / "runs"


def _path_for(run_id: str) -> Optional[Path]:
    if not run_id or not _SAFE_ID.match(run_id):
        return None
    return runs_dir() / f"{run_id}.json"


def save_run(record: dict) -> Optional[Path]:
    """Persist one run record; returns the file path (or None when unwritable)."""
    run_id = str(record.get("run_id") or "")
    path = _path_for(run_id)
    if path is None:
        logger.debug("Refusing to persist run with unsafe id %r", run_id)
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, default=str),
                       encoding="utf-8")
        tmp.replace(path)          # atomic: readers never see a half-written file
        return path
    except OSError as exc:
        logger.debug("Run record persist skipped: %s", exc)
        return None


def get_run(run_id: str) -> Optional[dict]:
    path = _path_for(run_id)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Run record read failed for %s: %s", run_id, exc)
        return None


def delete_run(run_id: str) -> bool:
    path = _path_for(run_id)
    if path is None or not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _summary(record: dict, mtime: float) -> dict:
    """Trim a full record down to what a history list needs."""
    usage = record.get("usage") or {}
    stats = record.get("task_stats") or {}
    verification = record.get("verification") or {}
    return {
        "run_id": record.get("run_id", ""),
        "session_id": record.get("session_id", ""),
        "request": (record.get("request") or "")[:200],
        "started_at": record.get("started_at", ""),
        "duration_seconds": record.get("duration_seconds", 0),
        "status": verification.get("status", record.get("status", "")),
        "verification_summary": verification.get("summary", ""),
        "tasks": stats.get("tasks", 0),
        "failed_tasks": stats.get("failed", 0),
        "retried_tasks": stats.get("retried", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "model_calls": usage.get("calls", 0),
        "cost_usd": usage.get("cost_usd"),
        "mtime": mtime,
    }


def list_runs(limit: int = 100, *, session_id: str = "") -> list[dict]:
    """Return run summaries, newest first."""
    directory = runs_dir()
    if not directory.exists():
        return []
    entries: list[tuple[float, Path]] = []
    for path in directory.glob("*.json"):
        if path.name.endswith(".json.tmp"):
            continue
        try:
            entries.append((path.stat().st_mtime, path))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)

    summaries: list[dict] = []
    for mtime, path in entries[: max(1, limit) * 3]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if session_id and record.get("session_id") != session_id:
            continue
        summaries.append(_summary(record, mtime))
        if len(summaries) >= max(1, limit):
            break
    return summaries


def overview(limit: int = 200) -> dict:
    """Aggregate the recent runs into a small dashboard payload."""
    summaries = list_runs(limit=limit)
    if not summaries:
        return {
            "runs": 0, "total_tokens": 0, "model_calls": 0, "total_cost_usd": None,
            "avg_duration": 0.0, "failed_tasks": 0, "retried_tasks": 0,
            "status_counts": {}, "pricing_configured": False,
        }

    status_counts: dict[str, int] = {}
    for item in summaries:
        status = item.get("status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1

    costs = [item["cost_usd"] for item in summaries if item.get("cost_usd") is not None]
    return {
        "runs": len(summaries),
        "total_tokens": sum(item.get("total_tokens") or 0 for item in summaries),
        "model_calls": sum(item.get("model_calls") or 0 for item in summaries),
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "avg_duration": round(
            sum(item.get("duration_seconds") or 0 for item in summaries) / len(summaries), 2
        ),
        "failed_tasks": sum(item.get("failed_tasks") or 0 for item in summaries),
        "retried_tasks": sum(item.get("retried_tasks") or 0 for item in summaries),
        "status_counts": status_counts,
        "pricing_configured": bool(costs) or bool(_pricing()),
        "generated_at": time.time(),
    }


def _pricing() -> dict:
    from backend.observability.trace import load_pricing

    return load_pricing()
