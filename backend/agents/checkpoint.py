"""Checkpointer factory for the orchestration graph.

The graph's resumability hinges on a checkpointer: every node boundary is a
durable snapshot, so a run survives a process restart (``AGENT_CHECKPOINT_BACKEND``).

Backends
--------
``sqlite`` (default)
    ``AsyncSqliteSaver`` under ``outputs/checkpoints/graph.sqlite``.  Single
    host, zero ops, survives restarts.
``memory``
    ``InMemorySaver`` — used by tests and by ``AGENT_CHECKPOINT_BACKEND=memory``.
``none``
    No checkpointer at all: the graph still runs, but ``interrupt()`` (human
    approval) is unavailable and nothing resumes after a crash.

Multi-replica note: SQLite is a single-host file.  For several replicas sharing
one run, point ``AGENT_CHECKPOINT_PATH`` at shared storage or install
``langgraph-checkpoint-redis`` and extend :func:`checkpointer` — the factory is
the only place that needs to change.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger("agents.checkpoint")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def checkpoint_backend() -> str:
    """Configured backend name, normalised."""
    raw = os.environ.get("AGENT_CHECKPOINT_BACKEND", "sqlite").strip().lower()
    if raw in ("sqlite", "memory", "none"):
        return raw
    logger.warning("Unknown AGENT_CHECKPOINT_BACKEND=%r, using sqlite", raw)
    return "sqlite"


def checkpoint_path() -> Path:
    """Resolved checkpoint database path."""
    raw = os.environ.get("AGENT_CHECKPOINT_PATH", "")
    path = Path(raw) if raw else _PROJECT_ROOT / "outputs" / "checkpoints" / "graph.sqlite"
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


@asynccontextmanager
async def checkpointer() -> AsyncIterator[object | None]:
    """Yield a checkpointer for the lifetime of one run.

    A fresh connection per run keeps SQLite's single-writer model simple; the
    file is the shared state, not the handle.
    """
    backend = checkpoint_backend()

    if backend == "none":
        yield None
        return

    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    # sqlite
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError:  # pragma: no cover - optional extra
        logger.warning(
            "langgraph-checkpoint-sqlite is not installed; "
            "falling back to an in-memory checkpointer (runs will not survive a restart)"
        )
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    path = checkpoint_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            await saver.setup()
            yield saver
    except Exception as exc:  # pragma: no cover - disk/permission problems
        logger.warning("SQLite checkpointer unavailable (%s); using memory", exc)
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
