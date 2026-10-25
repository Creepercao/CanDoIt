"""Run dispatch — inline (in-process) or Celery (broker-backed).

``MQ_BACKEND`` selects the execution mode:

``inline`` (default)
    The HTTP request drives the graph itself and forwards events to SSE.  No
    broker, no worker, no extra moving parts — this is what local development
    and the test suite use.

``celery``
    The HTTP request only enqueues a run and returns immediately; a worker
    process drives the graph and publishes to the event bus.  The client tails
    ``GET /api/runs/{run_id}/events`` and can reconnect at any time.

The inline path must stay the default: a missing RabbitMQ must never prevent
the application from starting or the tests from passing.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("mq.dispatch")

MQ_BACKEND = "inline"


def mq_backend() -> str:
    """Resolved backend name: ``inline`` or ``celery``."""
    raw = os.environ.get("MQ_BACKEND", "inline").strip().lower()
    if raw in ("celery", "async", "queue"):
        return "celery"
    if raw not in ("inline", ""):
        logger.warning("Unknown MQ_BACKEND=%r, falling back to inline", raw)
    return "inline"


def broker_url() -> str:
    """Celery broker URL (RabbitMQ by default)."""
    return os.environ.get("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")


def result_backend() -> str:
    """Celery result backend (Redis by default)."""
    return os.environ.get("CELERY_RESULT_BACKEND", os.environ.get("REDIS_URL", ""))


def _shared_bus_available() -> bool:
    """Whether the worker and the API can see the same event stream.

    In Celery mode the worker publishes events and the API process subscribes.
    Without a shared Redis those are two different in-memory buses, so the run
    would execute fine while the client saw nothing — it looks like a hang.
    """
    return bool(os.environ.get("REDIS_URL", "").strip())


async def dispatch_run(run_id: str, payload: dict[str, Any]) -> str | None:
    """Hand a run to the Celery worker.

    Returns the Celery task id, or ``None`` when running inline (the caller is
    then responsible for driving the graph).
    """
    if mq_backend() != "celery":
        return None

    if not _shared_bus_available():
        logger.error(
            "MQ_BACKEND=celery needs REDIS_URL so the worker's events reach the "
            "API; running run %s inline instead",
            run_id,
        )
        return None

    try:
        from backend.mq.celery_app import execute_run
    except Exception as exc:  # celery missing or broker misconfigured
        logger.error("Celery dispatch unavailable (%s); run inline instead", exc)
        return None

    try:
        task = execute_run.delay(run_id, payload)
        logger.info("Dispatched run %s to Celery (task %s)", run_id, task.id)
        return task.id
    except Exception as exc:
        logger.error("Celery dispatch failed (%s); run inline instead", exc)
        return None
