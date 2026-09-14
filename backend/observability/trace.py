"""Per-run tracing: model usage, task timings and the final run record.

A multi-agent run makes a dozen or more model calls across parallel workers;
without instrumentation there is no way to answer "what did this cost, which
agent is slow, how often does it fail".  This module provides that data with a
single hook:

* the orchestrator opens a :func:`track_run` scope per request;
* :func:`current_trace` exposes it through a ``ContextVar``, so every worker
  task (``asyncio.gather`` copies the context) sees the same trace;
* :class:`UsageCallback` is attached once, inside ``create_chat_model``, and
  harvests token usage from every call without touching any call site.

Cost is only reported when the operator supplies prices, because a built-in
price table would silently rot.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("observability.trace")

_trace: ContextVar[Optional["RunTrace"]] = contextvars.ContextVar("dsh_run_trace", default=None)
_agent: ContextVar[str] = contextvars.ContextVar("dsh_current_agent", default="")


# ── records ────────────────────────────────────────────────────────────


@dataclass
class ModelCall:
    """One model invocation."""

    model: str = ""
    agent: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "agent": self.agent,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "duration": round(self.duration, 3),
        }


@dataclass
class TaskRecord:
    """One scheduled agent task, including its retries."""

    agent: str
    task_id: str = ""
    attempts: int = 1
    duration: float = 0.0
    status: str = "done"
    result_key: str = ""
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "task_id": self.task_id,
            "attempts": self.attempts,
            "duration": round(self.duration, 2),
            "status": self.status,
            "result_key": self.result_key,
            "count": self.count,
        }


@dataclass
class RunTrace:
    """Everything worth remembering about one run."""

    run_id: str
    session_id: str = ""
    request: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    tasks: list[TaskRecord] = field(default_factory=list)
    calls: list[ModelCall] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # ── collection ─────────────────────────────────────────────────────

    def add_call(self, call: ModelCall) -> None:
        self.calls.append(call)

    def add_task(self, record: TaskRecord) -> None:
        self.tasks.append(record)

    def note(self, **values: Any) -> None:
        self.extra.update(values)

    # ── aggregation ────────────────────────────────────────────────────

    @property
    def duration(self) -> float:
        end = self.ended_at or time.time()
        return max(0.0, end - self.started_at)

    def usage(self) -> dict:
        by_agent: dict[str, dict] = {}
        input_tokens = output_tokens = total_tokens = 0
        for call in self.calls:
            input_tokens += call.input_tokens
            output_tokens += call.output_tokens
            total_tokens += call.total_tokens
            bucket = by_agent.setdefault(
                call.agent or "(未归属)", {"calls": 0, "input_tokens": 0,
                                           "output_tokens": 0, "total_tokens": 0}
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += call.input_tokens
            bucket["output_tokens"] += call.output_tokens
            bucket["total_tokens"] += call.total_tokens

        models: dict[str, int] = {}
        for call in self.calls:
            if call.model:
                models[call.model] = models.get(call.model, 0) + 1

        return {
            "calls": len(self.calls),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "by_agent": by_agent,
            "models": models,
            "cost_usd": estimate_cost(input_tokens, output_tokens),
            "pricing_configured": bool(load_pricing()),
        }

    def task_stats(self) -> dict:
        failed = [t for t in self.tasks if t.status != "done"]
        return {
            "tasks": len(self.tasks),
            "failed": len(failed),
            "retried": sum(1 for t in self.tasks if t.attempts > 1),
            "slowest": max(
                ({"agent": t.agent, "duration": round(t.duration, 2)} for t in self.tasks),
                key=lambda item: item["duration"],
                default=None,
            ),
        }

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "request": self.request,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at) if self.ended_at else "",
            "duration_seconds": round(self.duration, 2),
            "tasks": [t.to_dict() for t in self.tasks],
            "task_stats": self.task_stats(),
            "model_calls": [c.to_dict() for c in self.calls],
            "usage": self.usage(),
            **self.extra,
        }


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── pricing (operator supplied) ────────────────────────────────────────


def load_pricing() -> dict:
    """Parse ``AGENT_MODEL_PRICING`` (USD per 1M tokens).

    Expected shape::

        {"model-substring": {"input": 0.5, "output": 1.5}}

    Matching is by substring against the model id, longest key first.  No
    built-in table is shipped on purpose: stale prices are worse than none.
    """
    raw = os.environ.get("AGENT_MODEL_PRICING", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("AGENT_MODEL_PRICING is not valid JSON; ignoring")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): value for key, value in data.items() if isinstance(value, dict)
    }


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "") -> Optional[float]:
    """Blended cost estimate, or ``None`` when no pricing is configured."""
    pricing = load_pricing()
    if not pricing:
        return None
    entry = None
    if model:
        for key in sorted(pricing, key=len, reverse=True):
            if key and key.lower() in model.lower():
                entry = pricing[key]
                break
    if entry is None:
        entry = next(iter(pricing.values()), None)
    if not isinstance(entry, dict):
        return None
    try:
        cost = (input_tokens / 1_000_000) * float(entry.get("input", 0))
        cost += (output_tokens / 1_000_000) * float(entry.get("output", 0))
    except (TypeError, ValueError):
        return None
    return round(cost, 6)


# ── context plumbing ───────────────────────────────────────────────────


@contextmanager
def track_run(run_id: str, *, session_id: str = "", request: str = ""):
    """Open a trace scope for one run."""
    trace = RunTrace(run_id=run_id, session_id=session_id, request=request)
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        trace.ended_at = time.time()
        _trace.reset(token)


def current_trace() -> Optional[RunTrace]:
    return _trace.get()


def set_current_agent(name: str) -> None:
    """Tag subsequent model calls with an agent name (task-local)."""
    _agent.set(name or "")


def current_agent() -> str:
    return _agent.get()


# ── LangChain callback ─────────────────────────────────────────────────


def _extract_usage(response: Any) -> tuple[str, int, int, int, str]:
    """Return ``(model, input_tokens, output_tokens, total_tokens, agentish)``."""
    model = ""
    input_tokens = output_tokens = total_tokens = 0

    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        model = str(llm_output.get("model_name") or llm_output.get("model") or "")
        usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if isinstance(usage, dict):
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            total_tokens = int(usage.get("total_tokens") or 0)

    if not (input_tokens or output_tokens):
        for generation_list in getattr(response, "generations", None) or []:
            for generation in generation_list:
                message = getattr(generation, "message", None)
                meta = getattr(message, "usage_metadata", None) if message else None
                if isinstance(meta, dict):
                    input_tokens += int(meta.get("input_tokens") or 0)
                    output_tokens += int(meta.get("output_tokens") or 0)
                    total_tokens += int(meta.get("total_tokens") or 0)
                if not model and message is not None:
                    meta_response = getattr(message, "response_metadata", None)
                    if isinstance(meta_response, dict):
                        model = str(meta_response.get("model_name")
                                    or meta_response.get("model") or "")

    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return model, input_tokens, output_tokens, total_tokens, ""


def build_usage_callback():
    """Create the callback handler, or ``None`` when LangChain is unavailable."""
    try:
        from langchain_core.callbacks import AsyncCallbackHandler
    except Exception:  # pragma: no cover - langchain always present in practice
        return None

    class UsageCallback(AsyncCallbackHandler):
        """Harvest token usage from every model call in the current run."""

        def __init__(self) -> None:
            self._started: dict[str, float] = {}

        async def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: D102
            run_id = kwargs.get("run_id")
            if run_id is not None:
                self._started[str(run_id)] = time.monotonic()

        async def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: D102
            run_id = kwargs.get("run_id")
            if run_id is not None:
                self._started[str(run_id)] = time.monotonic()

        async def on_llm_end(self, response, **kwargs):  # noqa: D102
            trace = current_trace()
            started = self._started.pop(str(kwargs.get("run_id")), None)
            if trace is None:
                return
            model, input_tokens, output_tokens, total_tokens, _ = _extract_usage(response)
            trace.add_call(ModelCall(
                model=model,
                agent=current_agent(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                duration=(time.monotonic() - started) if started else 0.0,
            ))

        async def on_llm_error(self, error, **kwargs):  # noqa: D102
            self._started.pop(str(kwargs.get("run_id")), None)

    return UsageCallback()
