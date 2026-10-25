"""Tests for the message-queue layer.

These cover the three promises the queue makes:

1. an event stream can be resumed from the last id the client saw;
2. an approval decision can be handed to a paused run from another process;
3. ``MQ_BACKEND`` defaults to inline so no broker is needed to run anything.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.mq.bus import RunEventBus
from backend.mq.decisions import ApprovalStore


def _run(coro):
    return asyncio.run(coro)


# ── event bus ──────────────────────────────────────────────────────────


def test_bus_publishes_and_streams_in_order():
    bus = RunEventBus(redis_url="")

    async def scenario():
        first = await bus.publish("run_a", "phase", {"phase": "supervisor"})
        second = await bus.publish("run_a", "plan", {"count": 2})
        await bus.mark("run_a", "done")
        events = [e async for e in bus.stream("run_a")]
        return first, second, events

    first, second, events = _run(scenario())

    assert first and second
    kinds = [e["event"] for e in events if e["event"] != "heartbeat"]
    assert kinds == ["phase", "plan"]
    assert events[0]["data"]["phase"] == "supervisor"


def test_bus_resumes_after_last_seen_id():
    """A reconnecting client must receive only what it missed."""
    bus = RunEventBus(redis_url="")

    async def scenario():
        await bus.publish("run_b", "agent_start", {"agent": "research"})
        mid = await bus.publish("run_b", "agent_done", {"agent": "research"})
        await bus.publish("run_b", "final", {"response": "hi"})
        await bus.mark("run_b", "done")
        return mid, [e async for e in bus.stream("run_b", last_id=mid)]

    mid, events = _run(scenario())

    kinds = [e["event"] for e in events if e["event"] != "heartbeat"]
    assert kinds == ["final"], "the two events before the cursor must not replay"
    assert mid


def test_bus_status_reports_terminal_state():
    bus = RunEventBus(redis_url="")

    async def scenario():
        await bus.publish("run_c", "phase", {})
        running = await bus.status("run_c")
        await bus.mark("run_c", "error", "boom")
        return running, await bus.status("run_c")

    running, failed = _run(scenario())
    assert running == "running"
    assert failed == "error"


def test_bus_unknown_run_is_unknown():
    bus = RunEventBus(redis_url="")
    assert _run(bus.status("never-existed")) == "unknown"


def test_bus_stream_terminates_for_finished_run():
    """A finished run's stream must end instead of hanging forever."""
    bus = RunEventBus(redis_url="")

    async def scenario():
        await bus.publish("run_d", "done", {})
        await bus.mark("run_d", "done")
        return [e async for e in bus.stream("run_d", heartbeat=0.1, poll_interval=0.05)]

    events = _run(scenario())
    assert any(e["event"] == "done" for e in events)


# ── approval decisions ─────────────────────────────────────────────────


def test_approval_store_roundtrip():
    store = ApprovalStore(redis_url="")

    async def scenario():
        await store.put("appr_1", {"approved": True, "decided_by": "human"})
        return await store.wait("appr_1", timeout=1)

    decision = _run(scenario())
    assert decision is not None
    assert decision["approved"] is True


def test_approval_wait_times_out_cleanly():
    store = ApprovalStore(redis_url="")

    async def scenario():
        return await store.wait("appr_missing", timeout=0.2)

    assert _run(scenario()) is None


def test_approval_wait_is_woken_by_a_late_writer():
    store = ApprovalStore(redis_url="")

    async def scenario():
        async def writer():
            await asyncio.sleep(0.05)
            await store.put("appr_late", {"approved": False, "decided_by": "tester"})

        task = asyncio.create_task(writer())
        decision = await store.wait("appr_late", timeout=2)
        await task
        return decision

    decision = _run(scenario())
    assert decision is not None
    assert decision["approved"] is False


# ── dispatch ───────────────────────────────────────────────────────────


def test_dispatch_is_inline_by_default(monkeypatch):
    from backend.mq import dispatch

    monkeypatch.delenv("MQ_BACKEND", raising=False)
    assert dispatch.mq_backend() == "inline"
    assert _run(dispatch.dispatch_run("run_e", {})) is None


def test_dispatch_selects_celery_when_configured(monkeypatch):
    from backend.mq import dispatch

    monkeypatch.setenv("MQ_BACKEND", "celery")
    assert dispatch.mq_backend() == "celery"


def test_unknown_mq_backend_falls_back_to_inline(monkeypatch):
    from backend.mq import dispatch

    monkeypatch.setenv("MQ_BACKEND", "kafka-but-not-really")
    assert dispatch.mq_backend() == "inline"


def test_celery_without_a_shared_bus_runs_inline(monkeypatch):
    """Dispatching with no shared event bus would silently hang the client."""
    from backend.mq import dispatch

    monkeypatch.setenv("MQ_BACKEND", "celery")
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert dispatch._shared_bus_available() is False
    assert _run(dispatch.dispatch_run("run_no_bus", {})) is None


def test_celery_dispatch_calls_the_task_when_the_bus_exists(monkeypatch):
    import backend.mq.celery_app as ca
    from backend.mq import dispatch

    monkeypatch.setenv("MQ_BACKEND", "celery")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    class _FakeTask:
        id = "task-123"

        def delay(self, run_id, payload):
            return self

    monkeypatch.setattr(ca, "execute_run", _FakeTask())

    assert _run(dispatch.dispatch_run("run_bus", {})) == "task-123"


# ── HTTP surface ───────────────────────────────────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.main import app

    return TestClient(app)


def test_run_status_endpoint(client):
    bus = RunEventBus(redis_url="")
    import backend.mq.bus as bus_mod

    original = bus_mod._bus
    bus_mod._bus = bus
    try:
        _run(bus.publish("run_http", "phase", {"phase": "supervisor"}))
        _run(bus.mark("run_http", "done"))

        response = client.get("/api/runs/run_http/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "done"
        assert payload["run_id"] == "run_http"
    finally:
        bus_mod._bus = original


def test_run_events_endpoint_replays_and_stops(client):
    bus = RunEventBus(redis_url="")
    import backend.mq.bus as bus_mod

    original = bus_mod._bus
    bus_mod._bus = bus
    try:
        _run(bus.publish("run_sse", "phase", {"phase": "supervisor"}))
        _run(bus.publish("run_sse", "done", {}))
        _run(bus.mark("run_sse", "done"))

        response = client.get("/api/runs/run_sse/events")
        assert response.status_code == 200
        body = response.text
        assert "event: phase" in body
        assert "event: done" in body
        # The frame carries an id so EventSource can send Last-Event-ID.
        assert "id: " in body
    finally:
        bus_mod._bus = original


def test_submit_approval_endpoint_records_decision(client):
    store = ApprovalStore(redis_url="")
    import backend.mq.decisions as decisions_mod

    original = decisions_mod._store
    decisions_mod._store = store
    try:
        response = client.post(
            "/api/runs/run_x/approvals/appr_http",
            json={"approved": True, "decided_by": "tester"},
        )
        assert response.status_code == 200
        decision = _run(store.get("appr_http"))
        assert decision is not None
        assert decision["approved"] is True
        assert decision["run_id"] == "run_x"
    finally:
        decisions_mod._store = original


# ── Celery task body ───────────────────────────────────────────────────


def test_celery_task_is_registered():
    from backend.mq import celery_app as ca

    assert "candotit.execute_run" in ca.app.tasks


def test_celery_task_drives_a_run_eagerly(monkeypatch, safety_env, fake_skills):
    """Exercise the real worker task body without needing a broker.

    ``task_always_eager`` runs the task inline, which is enough to prove the
    task -> :func:`run_in_worker` -> graph -> event bus path works end to end.
    """
    import sys

    import backend.mq.bus as bus_mod
    from backend.mq.bus import RunEventBus

    bus = RunEventBus(redis_url="")
    original_bus = bus_mod._bus
    bus_mod._bus = bus

    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "memory")
    sys.modules.pop("backend.mq.celery_app", None)

    from backend.agents import orchestrator

    async def _supervisor(state):
        return {
            "tasks": [{"step": 1, "agent": "code", "prompt": "写个脚本"}],
            "plan_steps": [],
        }

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)

    import backend.mq.worker as worker_mod

    monkeypatch.setattr(worker_mod, "bootstrap", lambda: None)

    try:
        from backend.mq import celery_app as ca

        ca.app.conf.update(task_always_eager=True, task_eager_propagates=True)

        state = orchestrator.make_initial_state(user_request="写个脚本", chat_model_id="m")
        state["_skip_synthesizer"] = True
        run_id = state["run_id"]

        result = ca.execute_run.apply(args=[run_id, state]).get()

        assert result["run_id"] == run_id
        # The worker must publish the terminal frames itself, since no HTTP
        # request is alive to do it.
        events = _run(_collect_events(bus, run_id))
        kinds = [e["event"] for e in events]
        assert "done" in kinds
        assert "final" in kinds
    finally:
        bus_mod._bus = original_bus
        sys.modules.pop("backend.mq.celery_app", None)


async def _collect_events(bus, run_id):
    return [e async for e in bus.stream(run_id)]


def test_worker_does_not_double_emit_terminal_frames(
    monkeypatch, safety_env, fake_skills
):
    """A direct reply is published by the graph itself; the worker must not repeat it."""
    import sys

    import backend.mq.bus as bus_mod
    from backend.mq.bus import RunEventBus

    bus = RunEventBus(redis_url="")
    original_bus = bus_mod._bus
    bus_mod._bus = bus

    monkeypatch.setenv("AGENT_CHECKPOINT_BACKEND", "memory")
    sys.modules.pop("backend.mq.celery_app", None)

    from backend.agents import orchestrator

    async def _supervisor(state):
        return {"final_response": "直接回答", "tasks": [], "plan_steps": []}

    monkeypatch.setattr(orchestrator, "supervisor_node", _supervisor)

    import backend.mq.worker as worker_mod

    monkeypatch.setattr(worker_mod, "bootstrap", lambda: None)

    try:
        from backend.mq import celery_app as ca

        ca.app.conf.update(task_always_eager=True, task_eager_propagates=True)

        state = orchestrator.make_initial_state(user_request="你好", chat_model_id="m")
        run_id = state["run_id"]

        result = ca.execute_run.apply(args=[run_id, state]).get()

        assert result["terminal"] == "graph"
        kinds = [e["event"] for e in _run(_collect_events(bus, run_id))]
        assert kinds.count("final") == 1, kinds
        assert kinds.count("done") == 1, kinds
    finally:
        bus_mod._bus = original_bus
        sys.modules.pop("backend.mq.celery_app", None)

