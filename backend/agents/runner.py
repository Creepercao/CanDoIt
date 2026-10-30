"""Run driver — pumps the LangGraph backbone and publishes its events.

This is the seam the message queue plugs into.  :func:`stream_run` is a plain
async generator, so the same driver works in three places:

1. the HTTP request handler (``MQ_BACKEND=inline``), which forwards events to SSE;
2. a Celery worker (``MQ_BACKEND=celery``), which only publishes to the bus;
3. tests, which drive it directly.

Human approval arrives as a graph ``interrupt()``.  The driver publishes
``approval_required``, waits on the :class:`ApprovalStore`, then resumes the
graph with ``Command(resume=...)``.  Because the decision is stored outside the
process, the approving HTTP request does not have to be the one that started
the run — that is what makes "long task, no HTTP connection" work.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, AsyncGenerator

from langgraph.types import Command, Interrupt

from backend.agents.checkpoint import checkpointer
from backend.agents.graph_flow import (
    AgentState,
    compile_graph,
    recursion_limit,
    release_reviewer,
)
from backend.mq import get_event_bus
from backend.mq.decisions import get_approval_store

logger = logging.getLogger("agents.runner")


def _approval_timeout() -> float:
    """How long to wait for a human decision before failing closed.

    ``SAFETY_APPROVAL_TIMEOUT_SECONDS`` is the historical knob used by the
    safety layer, so it stays authoritative; ``AGENT_APPROVAL_TIMEOUT_SECONDS``
    lets a deployment override it for the graph path specifically.
    """
    raw = (
        os.environ.get("AGENT_APPROVAL_TIMEOUT_SECONDS")
        or os.environ.get("SAFETY_APPROVAL_TIMEOUT_SECONDS")
        or "600"
    )
    try:
        return max(0.5, float(raw))
    except (TypeError, ValueError):
        return 600.0


async def _pump(
    initial_state: dict,
    labels: dict[str, str],
    queue: asyncio.Queue,
) -> None:
    """Run the graph to completion, fanning events into *queue*."""
    run_id = initial_state.get("run_id") or ""
    bus = get_event_bus()
    store = get_approval_store()

    state = dict(initial_state)
    state["_agent_labels"] = labels
    state.setdefault("_streaming", True)

    final_state: dict[str, Any] = {}

    # Root span of the execution chain. Graph nodes attach to it by span id,
    # so it is opened here — the driver owns the run's lifetime.
    chain = None
    run_span = None
    try:
        from backend.agents.graph_flow import set_run_span
        from backend.observability import current_trace
        from backend.observability.chain import KIND_RUN, RunChain

        trace = current_trace()
        chain = trace.chain if trace is not None else RunChain.disabled()
        run_span = chain.start(
            KIND_RUN, "整个运行",
            request=(state.get("user_request") or "")[:120],
        )
        set_run_span(run_id, run_span.span_id)
    except Exception as exc:  # tracing must never break a run
        logger.debug("Run span skipped: %s", exc)

    try:
        async with checkpointer() as cp:
            graph = compile_graph(cp)

            # Open the audit trail for this run before any node touches it.
            try:
                from backend.agents.graph_flow import _reviewer_for

                _reviewer_for(state).record(
                    "run.start", stage="run", decision="allow", risk="low",
                    message="运行开始",
                    details={
                        "request": (state.get("user_request") or "")[:200],
                        "checkpoint_backend": os.environ.get(
                            "AGENT_CHECKPOINT_BACKEND", "sqlite"
                        ),
                    },
                )
            except Exception as exc:
                logger.debug("run.start audit skipped: %s", exc)
            config = {
                "configurable": {"thread_id": run_id},
                "recursion_limit": recursion_limit(),
            }

            payload: Any = state
            while True:
                interrupted: Interrupt | None = None
                async for chunk in graph.astream(
                    payload, config, stream_mode="updates"
                ):
                    if isinstance(chunk, dict) and "__interrupt__" in chunk:
                        interrupts = chunk["__interrupt__"] or ()
                        interrupted = interrupts[0] if interrupts else None

                if interrupted is None:
                    break

                value = interrupted.value if hasattr(interrupted, "value") else {}
                if not isinstance(value, dict):
                    value = {"detail": str(value)}
                approval_id = value.get("approval_id", "")

                # Announce it as outstanding so a client that reloads (or a
                # second operator) can still find and answer it.
                await store.register(approval_id, {
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "stage": value.get("stage", ""),
                    "agent": value.get("agent", ""),
                    "summary": value.get("summary", ""),
                    "risk": value.get("risk", ""),
                    "requested_at": time.time(),
                })
                await bus.publish(run_id, "approval_required", value)
                decision = await store.wait(approval_id, timeout=_approval_timeout())
                await store.resolve(approval_id)
                if decision is None:
                    # fail-closed: an unanswered approval is a rejection
                    decision = {"approved": False, "decided_by": "timeout"}
                    await bus.publish(run_id, "approval_resolved", {
                        "run_id": run_id,
                        "approval_id": approval_id,
                        "status": "rejected",
                        "decided_by": "timeout",
                    })
                await store.forget(approval_id)
                payload = Command(resume=decision)

            snapshot = await graph.aget_state(config)
            final_state = dict(snapshot.values or {})

    except asyncio.CancelledError:
        await bus.publish(run_id, "error", {"error": "run cancelled"})
        await bus.mark(run_id, "cancelled")
        raise
    except Exception as exc:
        logger.exception("Graph run failed: %s", exc)
        await bus.publish(run_id, "error", {"error": str(exc)})
        await bus.mark(run_id, "error", str(exc))
        final_state = dict(initial_state)
        final_state["final_response"] = ""
    finally:
        # The run summary must be attached before the reviewer is released —
        # every exit path (refusal included) is expected to carry ``_safety``.
        try:
            from backend.agents.graph_flow import _reviewer_for

            summary = _reviewer_for(final_state or state).summary()
        except Exception:
            summary = {}
        if chain is not None and run_span is not None:
            try:
                from backend.agents.orchestrator import _chain_status
                from backend.agents.graph_flow import clear_run_span

                chain.finish(
                    run_span,
                    status=_chain_status(final_state or state),
                    error=(final_state or state).get("_stopped_reason", ""),
                    verification=((final_state or state).get("_verification") or {}).get(
                        "status", ""
                    ),
                    tasks=len((final_state or state).get("tasks") or []),
                    blocks=len((final_state or state).get("safety_blocks") or []),
                )
                clear_run_span(run_id)
            except Exception as exc:
                logger.debug("Run span finish skipped: %s", exc)
        release_reviewer(run_id)

    if not final_state:
        final_state = dict(initial_state)
    final_state.setdefault("_safety", summary)

    # Closing phase frame — the SSE layer uses it to switch the UI into
    # "synthesizing" mode before the final answer streams in. A direct reply
    # already published its own terminal frames, so this would announce a
    # phase that never happens.
    if not final_state.get("_terminal_emitted"):
        await bus.publish(run_id, "phase", {"phase": "synthesize", "message": "synthesizing..."})

    queue.put_nowait({
        "event": "_agent_loop_done",
        "data": {
            "state": final_state,
            "accumulated": final_state.get("_accumulated") or {},
            "collected_html": final_state.get("_collected_html") or [],
            "run_id": run_id,
            "safety": final_state.get("_safety") or {},
            "dropped_tasks": final_state.get("dropped_tasks") or [],
            "safety_blocks": final_state.get("safety_blocks") or [],
            "stopped_reason": final_state.get("_stopped_reason") or "",
        },
    })
    queue.put_nowait(None)


async def stream_run(
    initial_state: dict,
    labels: dict[str, str] | None = None,
) -> AsyncGenerator[dict, None]:
    """Drive the graph and yield each event as it is published.

    The terminal ``_agent_loop_done`` event carries the final state, matching
    the contract the SSE layer already expects.
    """
    run_id = initial_state.get("run_id") or ""
    bus = get_event_bus()
    queue = bus.subscribe_local(run_id)

    # Mark the run as live before the first node publishes anything.
    await bus.mark(run_id, "running")

    task = asyncio.create_task(
        _pump(initial_state, labels or {}, queue),
        name=f"run:{run_id}",
    )
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        else:
            exc = task.exception()
            if exc is not None:
                logger.error("Run task failed: %s", exc)
        bus.unsubscribe_local(run_id, queue)


async def run_graph(initial_state: dict, labels: dict[str, str] | None = None) -> dict:
    """Non-streaming view over :func:`stream_run` — returns the final state."""
    state: dict = dict(initial_state)
    async for event in stream_run(initial_state, labels):
        if event.get("event") == "_agent_loop_done":
            state = event["data"].get("state") or state
    return state
