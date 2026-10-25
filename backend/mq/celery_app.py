"""Celery application for ``MQ_BACKEND=celery``.

Broker defaults to RabbitMQ, result backend to Redis.  Neither is required for
the default ``inline`` mode — this module is only imported when a run is
actually dispatched to a worker.
"""
from __future__ import annotations

import logging

from celery import Celery

from backend.mq.dispatch import broker_url, result_backend

logger = logging.getLogger("mq.celery")

app = Celery(
    "candotit",
    broker=broker_url(),
    backend=result_backend() or None,
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Long research/PPT runs must not be re-delivered mid-flight; ack late,
    # take one task at a time.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Runs can legitimately exceed an hour (PPT pipelines on a slow provider).
    task_time_limit=int(__import__("os").environ.get("MQ_TASK_TIME_LIMIT", "7200")),
    task_soft_time_limit=None,
)


@app.task(name="candotit.execute_run", bind=True)
def execute_run(self, run_id: str, payload: dict) -> dict:
    """Celery task: drive one run to completion inside the worker."""
    from backend.mq.worker import run_sync

    logger.info("Worker picked up run %s (task %s)", run_id, self.request.id)
    return run_sync(payload)
