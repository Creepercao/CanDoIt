"""Celery worker entry point for ``MQ_BACKEND=celery``.

The worker owns the whole run when dispatched: it drives the graph, publishes
every event to the bus, and (unlike the inline path) also performs synthesis
and emits the terminal ``final``/``done`` frames — because no HTTP request is
alive to do it.

Windows note: Celery's default prefork pool is unavailable there.  Run the
worker with ``--pool=solo`` (or ``threads``); see ``docker-compose.yml`` and
the README for the exact command.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("mq.worker")


def bootstrap() -> None:
    """Load agent skills into the registry inside a fresh worker process."""
    try:
        from backend.skills.registry import skill_registry

        skill_registry.discover()
    except Exception as exc:  # registry discovery is best-effort
        logger.warning("Skill registry bootstrap failed: %s", exc)


async def run_in_worker(payload: dict) -> dict:
    """Drive one run inside the worker and publish its terminal frames."""
    from backend.agents.runner import stream_run
    from backend.mq import get_event_bus

    bus = get_event_bus()
    run_id = payload.get("run_id", "")

    labels: dict[str, str] = {}
    try:
        from backend.skills.registry import skill_registry

        labels = {
            name: skill.display_name
            for name, skill in skill_registry.get_enabled().items()
        }
    except Exception:
        labels = {}

    final_state: dict = dict(payload)
    async for event in stream_run(payload, labels):
        if event.get("event") == "_agent_loop_done":
            final_state = event["data"].get("state") or final_state

    response = final_state.get("final_response") or ""

    # Direct replies already published ``final``/``done`` inside the graph.
    # Publishing again would make the client render the answer twice.
    if final_state.get("_terminal_emitted"):
        await bus.mark(run_id, "done")
        return {"run_id": run_id, "response_chars": len(response), "terminal": "graph"}

    if not response and not final_state.get("_skip_synthesizer"):
        response = await _synthesize(final_state, bus, run_id)

    collected_html = final_state.get("_collected_html") or []
    verification = final_state.get("_verification") or {}

    await bus.publish(run_id, "final", {
        "response": response,
        "chart_results": final_state.get("chart_results", []),
        "image_results": final_state.get("image_results", []),
        "html_results": collected_html,
        "run_id": run_id,
        "safety": final_state.get("_safety") or {},
        "verification": verification,
        "safety_blocks": final_state.get("safety_blocks") or [],
        "recalled_memories": final_state.get("_recalled_memories", []),
    })
    await bus.publish(run_id, "done", {})
    await bus.mark(run_id, "done")

    return {"run_id": run_id, "response_chars": len(response)}


async def _synthesize(state: dict, bus, run_id: str) -> str:
    """Stream the final answer, publishing token events as they arrive."""
    from langchain_core.messages import HumanMessage

    try:
        from backend.api.chat import _build_synth_prompt
        from backend.models.provider import create_chat_model
    except Exception as exc:
        logger.error("Synthesis unavailable in worker: %s", exc)
        return ""

    await bus.publish(run_id, "phase", {"phase": "synthesize", "message": "synthesizing..."})

    prompt = _build_synth_prompt(
        state.get("user_request", ""), state, state.get("messages") or []
    )
    model = create_chat_model(
        model_id=state.get("chat_model_id") or "deepseek-ai/DeepSeek-V3",
        temperature=0.7,
        max_tokens=4096,
        provider_config=None,
    )

    chunks: list[str] = []
    try:
        async for chunk in model.astream([HumanMessage(content=prompt)]):
            text = chunk.content if hasattr(chunk, "content") else str(chunk)
            if text:
                chunks.append(text)
                await bus.publish(run_id, "token", {"text": text})
    except Exception as exc:
        logger.error("Synthesis streaming failed: %s", exc)
        await bus.publish(run_id, "error", {"error": f"Synthesis failed: {exc}"})

    return "".join(chunks)


def run_sync(payload: dict) -> dict:
    """Blocking wrapper for Celery tasks."""
    bootstrap()
    return asyncio.run(run_in_worker(payload))
