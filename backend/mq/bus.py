"""Run event bus — Redis Streams with an in-memory fallback.

A run's SSE events are appended to a per-run stream and given a monotonically
increasing entry id.  A client that loses its connection can reconnect with
``?last_id=<the id it last saw>`` and receive only what it missed — that is the
whole point of routing events through a bus instead of streaming them straight
out of the request handler.

Both execution modes (in-process and Celery worker) publish through this same
interface, so resume-after-disconnect behaves identically in either one.  When
no ``REDIS_URL`` is configured the bus degrades to a per-process ring buffer:
resume still works within one process, which is exactly the scope an
unconfigured local dev server has.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncGenerator

logger = logging.getLogger("mq.bus")

try:  # redis is an optional dependency at runtime (memory fallback exists)
    import redis.asyncio as aioredis

    REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover - redis is in requirements
    aioredis = None  # type: ignore[assignment]
    REDIS_AVAILABLE = False


# Statuses that mean "no more events will arrive for this run".
TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})

# How long a finished run's stream is kept for late reconnects.
RUN_TTL_SECONDS = int(os.environ.get("MQ_RUN_TTL_SECONDS", "3600"))

# Hard cap on stored events per run, so a runaway run cannot fill Redis.
MAX_EVENTS_PER_RUN = int(os.environ.get("MQ_MAX_EVENTS_PER_RUN", "5000"))


def _events_key(run_id: str) -> str:
    return f"run:events:{run_id}"


def _status_key(run_id: str) -> str:
    return f"run:status:{run_id}"


class _MemoryRun:
    """Per-run in-memory stream used when Redis is unavailable."""

    __slots__ = ("events", "status", "error", "wakeup")

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.status: str = "running"
        self.error: str = ""
        self.wakeup = asyncio.Event()


class RunEventBus:
    """Publish and tail the event stream of a run.

    Parameters
    ----------
    redis_url:
        Redis connection string.  Empty falls back to ``REDIS_URL``, then to
        the in-memory implementation.
    """

    def __init__(self, redis_url: str = "") -> None:
        self._redis = None
        self._memory: dict[str, _MemoryRun] = {}
        self._mem_seq = 0
        # In-process subscribers (queue fan-out) so a local generator can
        # consume the same events that are being written to the stream.
        self._local_subs: dict[str, list[asyncio.Queue]] = {}

        url = redis_url or os.environ.get("REDIS_URL", "")
        if REDIS_AVAILABLE and url:
            try:
                self._redis = aioredis.from_url(url, decode_responses=True)
            except Exception as exc:  # pragma: no cover - bad URL
                logger.warning("Event bus falling back to memory: %s", exc)
                self._redis = None

    # ── capability ────────────────────────────────────────────────────

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    # ── memory helpers ────────────────────────────────────────────────

    def _mem(self, run_id: str) -> _MemoryRun:
        run = self._memory.get(run_id)
        if run is None:
            run = _MemoryRun()
            self._memory[run_id] = run
        return run

    # ── publishing ────────────────────────────────────────────────────

    def subscribe_local(self, run_id: str) -> asyncio.Queue:
        """Register an in-process queue that receives this run's events.

        Used by the streaming entry point, which must both persist events (for
        resume) and hand them to the current caller.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._local_subs.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe_local(self, run_id: str, queue: asyncio.Queue) -> None:
        subs = self._local_subs.get(run_id)
        if not subs:
            return
        try:
            subs.remove(queue)
        except ValueError:
            pass
        if not subs:
            self._local_subs.pop(run_id, None)

    def _fan_out_local(self, run_id: str, item: dict) -> None:
        for queue in list(self._local_subs.get(run_id, [])):
            try:
                queue.put_nowait(item)
            except Exception:  # pragma: no cover - full/unbounded queue
                pass

    async def publish(self, run_id: str, event: str, data: dict | None = None) -> str:
        """Append one event; returns its stream id.

        Publishing to a finished run is allowed on purpose: a late ``done``
        marker must not be dropped just because the status was already set.
        """
        payload = json.dumps(data or {}, ensure_ascii=False, default=str)

        if self._redis is not None:
            try:
                entry_id = await self._redis.xadd(
                    _events_key(run_id),
                    {"event": event, "data": payload},
                    maxlen=MAX_EVENTS_PER_RUN,
                    approximate=True,
                )
                await self._redis.expire(_events_key(run_id), RUN_TTL_SECONDS)
                self._fan_out_local(
                    run_id,
                    {"id": str(entry_id), "event": event, "data": data or {}},
                )
                return str(entry_id)
            except Exception as exc:
                logger.warning("Event bus publish failed, using memory: %s", exc)

        run = self._mem(run_id)
        self._mem_seq += 1
        entry_id = f"0-{self._mem_seq}"
        run.events.append({"id": entry_id, "event": event, "data": payload})
        # Trim from the front so a long run cannot grow without bound.
        overflow = len(run.events) - MAX_EVENTS_PER_RUN
        if overflow > 0:
            del run.events[:overflow]
        run.wakeup.set()
        self._fan_out_local(
            run_id, {"id": entry_id, "event": event, "data": data or {}}
        )
        return entry_id

    async def mark(self, run_id: str, status: str, error: str = "") -> None:
        """Record the terminal status of a run."""
        if self._redis is not None:
            try:
                await self._redis.setex(
                    _status_key(run_id),
                    RUN_TTL_SECONDS,
                    json.dumps({"status": status, "error": error}),
                )
                return
            except Exception as exc:
                logger.warning("Event bus mark failed, using memory: %s", exc)

        run = self._mem(run_id)
        run.status = status
        run.error = error
        run.wakeup.set()

    async def status(self, run_id: str) -> str:
        """Return ``running`` / ``done`` / ``error`` / ``cancelled`` / ``unknown``."""
        if self._redis is not None:
            try:
                raw = await self._redis.get(_status_key(run_id))
                if raw is None:
                    # No status key: either unknown, or a run that has events
                    # but never marked itself (crash) — report running so the
                    # client keeps waiting instead of silently giving up.
                    if await self._redis.exists(_events_key(run_id)):
                        return "running"
                    return "unknown"
                return json.loads(raw).get("status", "running")
            except Exception as exc:
                logger.warning("Event bus status failed, using memory: %s", exc)

        run = self._memory.get(run_id)
        return run.status if run else "unknown"

    async def latest_id(self, run_id: str) -> str:
        """Id of the newest event, or ``""`` when the run has none."""
        if self._redis is not None:
            try:
                entries = await self._redis.xrevrange(_events_key(run_id), count=1)
                return str(entries[0][0]) if entries else ""
            except Exception as exc:
                logger.warning("Event bus latest_id failed, using memory: %s", exc)

        run = self._memory.get(run_id)
        return run.events[-1]["id"] if run and run.events else ""

    # ── consumption ───────────────────────────────────────────────────

    async def stream(
        self,
        run_id: str,
        last_id: str = "0-0",
        *,
        heartbeat: float = 15.0,
        poll_interval: float = 0.5,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield events after *last_id*, then terminate once the run ends.

        Yields ``{"id", "event", "data"}`` for real events and
        ``{"event": "heartbeat"}`` when idle, so the SSE layer can keep the
        connection warm without inventing its own timer.
        """
        if self._redis is not None:
            async for item in self._stream_redis(
                run_id, last_id, heartbeat=heartbeat, poll_interval=poll_interval
            ):
                yield item
            return

        async for item in self._stream_memory(
            run_id, last_id, heartbeat=heartbeat, poll_interval=poll_interval
        ):
            yield item

    async def _stream_redis(
        self,
        run_id: str,
        last_id: str,
        *,
        heartbeat: float,
        poll_interval: float,
    ) -> AsyncGenerator[dict[str, Any], None]:
        key = _events_key(run_id)
        cursor = last_id or "0-0"

        # Replay whatever already happened before the client attached.
        while True:
            try:
                entries = await self._redis.xrange(
                    key, min=f"({cursor}" if cursor not in ("0", "0-0") else "-",
                    max="+", count=200,
                )
            except Exception as exc:
                logger.warning("Event bus replay failed: %s", exc)
                entries = []

            for entry_id, fields in entries:
                cursor = str(entry_id)
                event = {"id": cursor, "event": fields.get("event", ""),
                         "data": json.loads(fields.get("data") or "{}")}
                yield event
                if event["event"] in ("done", "final") and await self._is_terminal(run_id):
                    # ``final`` is emitted before ``done``; keep draining so the
                    # client still sees every event of the final batch.
                    pass

            if len(entries) < 200:
                break

        idle = 0.0
        while True:
            if await self._is_terminal(run_id) and not await self._has_after(run_id, cursor):
                yield {"event": "heartbeat"}
                return

            try:
                response = await self._redis.xread(
                    {key: cursor}, count=100, block=int(heartbeat * 1000)
                )
            except Exception as exc:
                logger.warning("Event bus read failed: %s", exc)
                response = None

            if not response:
                idle += heartbeat
                yield {"event": "heartbeat"}
                if idle >= poll_interval and await self._is_terminal(run_id):
                    if not await self._has_after(run_id, cursor):
                        return
                continue

            idle = 0.0
            for _stream, entries in response:
                for entry_id, fields in entries:
                    cursor = str(entry_id)
                    yield {
                        "id": cursor,
                        "event": fields.get("event", ""),
                        "data": json.loads(fields.get("data") or "{}"),
                    }

    async def _is_terminal(self, run_id: str) -> bool:
        return await self.status(run_id) in TERMINAL_STATUSES

    async def _has_after(self, run_id: str, cursor: str) -> bool:
        if cursor in ("", "0", "0-0"):
            entries = await self._redis.xrange(_events_key(run_id), min="-", max="+", count=1)
        else:
            entries = await self._redis.xrange(_events_key(run_id), min=f"({cursor}", max="+", count=1)
        return bool(entries)

    async def _stream_memory(
        self,
        run_id: str,
        last_id: str,
        *,
        heartbeat: float,
        poll_interval: float,
    ) -> AsyncGenerator[dict[str, Any], None]:
        cursor = last_id or ""
        emitted = 0
        last_beat = time.monotonic()

        while True:
            run = self._memory.get(run_id)

            if run is not None:
                pending = [
                    item for item in run.events
                    if _mem_id_after(item["id"], cursor)
                ]
                for item in pending:
                    cursor = item["id"]
                    emitted += 1
                    yield {"id": item["id"], "event": item["event"],
                           "data": json.loads(item["data"])}
                if pending:
                    last_beat = time.monotonic()

            if run is None or run.status in TERMINAL_STATUSES:
                # Give the writer one more chance to flush a trailing event.
                if run is not None and run.status in TERMINAL_STATUSES:
                    trailing = [
                        item for item in run.events
                        if _mem_id_after(item["id"], cursor)
                    ]
                    if not trailing:
                        yield {"event": "heartbeat"}
                        return

            now = time.monotonic()
            if now - last_beat >= heartbeat:
                last_beat = now
                yield {"event": "heartbeat"}

            wait_event = run.wakeup if run is not None else None
            if wait_event is not None:
                wait_event.clear()
                try:
                    await asyncio.wait_for(wait_event.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(poll_interval)

    # ── cleanup ───────────────────────────────────────────────────────

    async def forget(self, run_id: str) -> None:
        """Drop a run's events and status (used by tests and deletes)."""
        if self._redis is not None:
            try:
                await self._redis.delete(_events_key(run_id), _status_key(run_id))
                return
            except Exception as exc:
                logger.warning("Event bus forget failed, using memory: %s", exc)
        self._memory.pop(run_id, None)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # pragma: no cover
                pass


def _mem_id_after(candidate: str, cursor: str) -> bool:
    """Compare in-memory ids (``0-<n>``) against a cursor.

    The cursor may be empty (from the beginning), a memory id, or a Redis
    entry id left over from a previous redis-backed run — in that last case we
    treat it as "from the beginning" rather than dropping everything.
    """
    if not cursor:
        return True
    try:
        return int(candidate.split("-")[-1]) > int(cursor.split("-")[-1])
    except (ValueError, IndexError):
        return True


_bus = RunEventBus()


def get_event_bus() -> RunEventBus:
    """Return the process-wide event bus."""
    return _bus
