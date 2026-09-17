"""Observability layer — per-run usage, timings and run history.

``trace`` collects model token usage and task timings for the current run;
``store`` persists the finished record and answers history queries.
"""

from __future__ import annotations

from backend.observability.chain import (
    KIND_ATTEMPT,
    KIND_EXECUTE,
    KIND_MEMORY,
    KIND_PLAN,
    KIND_REMEDIATE,
    KIND_REVIEW,
    KIND_RUN,
    KIND_SYNTH,
    KIND_TASK,
    KIND_VERIFY,
    RunChain,
    Span,
)
from backend.observability.store import (
    delete_run,
    get_run,
    list_runs,
    overview,
    save_run,
)
from backend.observability.trace import (
    ModelCall,
    RunTrace,
    TaskRecord,
    build_usage_callback,
    current_agent,
    current_trace,
    estimate_cost,
    load_pricing,
    set_current_agent,
    track_run,
)

__all__ = [
    "KIND_ATTEMPT",
    "KIND_EXECUTE",
    "KIND_MEMORY",
    "KIND_PLAN",
    "KIND_REMEDIATE",
    "KIND_REVIEW",
    "KIND_RUN",
    "KIND_SYNTH",
    "KIND_TASK",
    "KIND_VERIFY",
    "RunChain",
    "Span",
    "ModelCall",
    "RunTrace",
    "TaskRecord",
    "build_usage_callback",
    "current_agent",
    "current_trace",
    "delete_run",
    "estimate_cost",
    "get_run",
    "list_runs",
    "load_pricing",
    "overview",
    "save_run",
    "set_current_agent",
    "track_run",
]
