"""Approval decisions for graph interrupts.

When the graph calls ``interrupt()`` the run pauses *inside* the checkpointer,
not inside a coroutine holding an ``asyncio.Event``.  Something outside must
therefore hand the decision back.  That something is this store:

* ``put``   — called by the approval REST endpoint (possibly another process)
* ``wait``  — called by the run driver, which then resumes the graph

Redis makes the hand-off work across processes (API process approves, Celery
worker resumes).  The in-process ``asyncio.Event`` only shortens the polling
latency for the single-process case.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("mq.decisions")

try:
    import redis.asyncio as aioredis

    REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]
    REDIS_AVAILABLE = False


DECISION_TTL_SECONDS = int(os.environ.get("MQ_APPROVAL_TTL_SECONDS", "900"))


def _key(approval_id: str) -> str:
    return f"run:approval:{approval_id}"


class ApprovalStore:
    """Stores human decisions keyed by approval id."""

    def __init__(self, redis_url: str = "") -> None:
        self._redis = None
        self._memory: dict[str, dict[str, Any]] = {}
        self._events: dict[str, asyncio.Event] = {}

        url = redis_url or os.environ.get("REDIS_URL", "")
        if REDIS_AVAILABLE and url:
            try:
                self._redis = aioredis.from_url(url, decode_responses=True)
            except Exception as exc:  # pragma: no cover
                logger.warning("Approval store falling back to memory: %s", exc)
                self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def _event(self, approval_id: str) -> asyncio.Event:
        event = self._events.get(approval_id)
        if event is None:
            event = asyncio.Event()
            self._events[approval_id] = event
        return event

    async def put(self, approval_id: str, decision: dict) -> None:
        """Record a decision and wake any local waiter."""
        payload = json.dumps(decision, ensure_ascii=False, default=str)
        if self._redis is not None:
            try:
                await self._redis.setex(_key(approval_id), DECISION_TTL_SECONDS, payload)
            except Exception as exc:
                logger.warning("Approval put failed, using memory: %s", exc)
                self._memory[approval_id] = decision
        else:
            self._memory[approval_id] = decision
        self._event(approval_id).set()

    async def get(self, approval_id: str) -> dict | None:
        if self._redis is not None:
            try:
                raw = await self._redis.get(_key(approval_id))
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                logger.warning("Approval get failed, using memory: %s", exc)
        return self._memory.get(approval_id)

    async def wait(self, approval_id: str, timeout: float | None = None) -> dict | None:
        """Block until a decision arrives (or the timeout expires)."""
        deadline = time.monotonic() + (timeout or DECISION_TTL_SECONDS)

        decision = await self.get(approval_id)
        if decision is not None:
            return decision

        event = self._event(approval_id)
        while time.monotonic() < deadline:
            remaining = min(0.5, max(0.0, deadline - time.monotonic()))
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            decision = await self.get(approval_id)
            if decision is not None:
                return decision
            if event.is_set():
                event.clear()

        return None

    async def forget(self, approval_id: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(_key(approval_id))
            except Exception:  # pragma: no cover
                pass
        self._memory.pop(approval_id, None)
        self._events.pop(approval_id, None)


_store = ApprovalStore()


def get_approval_store() -> ApprovalStore:
    return _store
