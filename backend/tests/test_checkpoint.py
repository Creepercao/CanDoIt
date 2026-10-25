"""Tests for the checkpointer factory.

The point of the factory is that a run's state survives the process: SQLite by
default, memory for tests, and ``none`` for runs that must not be resumable.
"""
from __future__ import annotations

import asyncio

from backend.agents.checkpoint import checkpoint_backend, checkpoint_path, checkpointer


def _run(coro):
    return asyncio.run(coro)


def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.delenv("AGENT_CHECKPOINT_BACKEND", raising=False)
    assert checkpoint_backend() == "sqlite"


def test_unknown_backend_falls_back_to_sqlite(monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "tape-drive")
    assert checkpoint_backend() == "sqlite"


def test_sqlite_checkpointer_creates_its_database(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CHECKPOINT_PATH", str(tmp_path / "cp.sqlite"))

    async def scenario():
        async with checkpointer() as cp:
            return cp

    saver = _run(scenario())
    assert saver is not None
    # setup() must have created the sqlite file on disk.
    assert checkpoint_path().exists()


def test_memory_backend_returns_in_memory_saver(monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "memory")

    async def scenario():
        async with checkpointer() as cp:
            return cp

    saver = _run(scenario())
    assert saver is not None
    assert type(saver).__name__ == "InMemorySaver"


def test_none_backend_disables_checkpointing(monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "none")

    async def scenario():
        async with checkpointer() as cp:
            return cp

    assert _run(scenario()) is None
