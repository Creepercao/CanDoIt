"""Asynchronous run API — submit a run, then tail its events.

This is the queue-facing half of the chat surface.  It exists so a long run is
no longer bound to the HTTP connection that started it:

* ``POST /runs``                     submit a run, return its id immediately
* ``GET  /runs/{run_id}/events``     SSE tail, resumable via ``Last-Event-ID``
* ``GET  /runs/{run_id}/status``     running / done / error
* ``POST /runs/{run_id}/approvals/{approval_id}``  answer a graph interrupt

The stream is backed by :mod:`backend.mq.bus`, so reconnecting with the id of
the last frame received replays exactly the missed events.  Both execution
modes (``inline`` background task and Celery worker) publish to the same bus,
which is why resume works in either one.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.mq import get_event_bus, mq_backend
from backend.mq.decisions import get_approval_store
from backend.mq.dispatch import dispatch_run

logger = logging.getLogger("api.runs_async")

# asyncio keeps only a weak reference to tasks, so a fire-and-forget inline run
# can be garbage collected mid-flight. Hold a strong reference until it ends.
_BACKGROUND_TASKS: set = set()

router = APIRouter()


class AsyncRunRequest(BaseModel):
    """Same shape as a chat request, minus the streaming flag."""

    message: str
    chat_model_id: str = ""
    image_model_id: str = ""
    video_model_id: str = ""
    session_id: str = ""
    history: list = []


class ApprovalDecision(BaseModel):
    approved: bool
    decided_by: str = "human"
    note: str = ""


def _sse(event: str, data: dict, event_id: str = "") -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, default=str)}")
    return "\n".join(lines) + "\n\n"


@router.post("/runs")
async def create_run(req: AsyncRunRequest):
    """Enqueue a run and return immediately with its id."""
    from backend.api.chat import ChatRequest, _make_state

    chat_req = ChatRequest(
        message=req.message,
        chat_model_id=req.chat_model_id,
        image_model_id=req.image_model_id,
        video_model_id=req.video_model_id,
        session_id=req.session_id,
        history=req.history,
        stream=True,
    )
    state = _make_state(chat_req, skip_synthesizer=False)
    run_id = state.get("run_id", "")
    if not run_id:
        raise HTTPException(status_code=500, detail="run id allocation failed")

    bus = get_event_bus()
    await bus.mark(run_id, "running")

    task_id = await dispatch_run(run_id, state)
    if task_id is None:
        # Inline mode: drive it in the background, events still go to the bus.
        import asyncio

        from backend.mq.worker import run_in_worker

        task = asyncio.create_task(
            _run_inline(run_id, state),
            name=f"inline-run:{run_id}",
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        logger.info("Run %s started inline", run_id)

    return {"run_id": run_id, "task_id": task_id, "mode": mq_backend()}


async def _run_inline(run_id: str, state: dict) -> None:
    """Background driver shared with the Celery worker path."""
    from backend.mq.worker import run_in_worker

    try:
        await run_in_worker(state)
    except Exception as exc:
        logger.exception("Inline run %s failed", run_id)
        try:
            bus = get_event_bus()
            await bus.publish(run_id, "error", {"error": str(exc)})
            await bus.publish(run_id, "done", {})
            await bus.mark(run_id, "error", str(exc))
        except Exception:  # pragma: no cover
            pass


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, last_id: str = ""):
    """Tail a run's event stream, resumable from ``last_id``.

    The ``id:`` field on each frame lets ``EventSource`` reconnect on its own
    and send ``Last-Event-ID`` — clients that do not use EventSource can pass
    ``?last_id=`` explicitly.
    """
    bus = get_event_bus()

    async def generate() -> AsyncGenerator[str, None]:
        try:
            async for item in bus.stream(run_id, last_id or "0-0"):
                event = item.get("event", "")
                if event == "heartbeat":
                    yield ": heartbeat\n\n"
                    continue
                yield _sse(event, item.get("data") or {}, item.get("id", ""))
        except Exception as exc:  # a broken client must not spam the log
            logger.debug("Run stream %s ended: %s", run_id, exc)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/status")
async def run_status(run_id: str):
    """Current status plus the id of the newest event."""
    bus = get_event_bus()
    return {
        "run_id": run_id,
        "status": await bus.status(run_id),
        "latest_event_id": await bus.latest_id(run_id),
        "backend": bus.backend,
    }


@router.post("/runs/{run_id}/approvals/{approval_id}")
async def submit_approval(run_id: str, approval_id: str, decision: ApprovalDecision):
    """Answer a graph interrupt so the paused run can resume."""
    store = get_approval_store()
    await store.put(approval_id, {
        "approved": decision.approved,
        "decided_by": decision.decided_by,
        "note": decision.note,
        "run_id": run_id,
    })
    logger.info(
        "Approval %s for run %s: %s",
        approval_id, run_id, "approved" if decision.approved else "rejected",
    )
    return {"approval_id": approval_id, "status": "recorded"}
