"""Message-queue layer: run event bus + Celery dispatch.

Two independent concerns live here:

* :mod:`backend.mq.bus` — the run event bus.  Every SSE event produced by a
  run is published to a per-run stream so a client can disconnect and resume
  with the last event id it saw.
* :mod:`backend.mq.celery_app` — optional Celery dispatch, used when
  ``MQ_BACKEND=celery``.  The default ``inline`` mode keeps the original
  in-process behaviour so local development and the test suite need no broker.
"""

from backend.mq.bus import RunEventBus, get_event_bus
from backend.mq.dispatch import (
    MQ_BACKEND,
    dispatch_run,
    mq_backend,
)

__all__ = [
    "RunEventBus",
    "get_event_bus",
    "MQ_BACKEND",
    "mq_backend",
    "dispatch_run",
]
