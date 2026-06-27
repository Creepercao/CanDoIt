"""Compatibility wrapper — delegates to ``backend.agents.orchestrator``.

The project has been migrated from LangGraph StateGraph to an explicit
Agent Loop pattern.  This module exists so that any lingering imports of
``get_multi_agent_graph`` or ``build_graph`` still work, but all actual
orchestration now lives in ``orchestrator.py``.
"""

from __future__ import annotations

import logging

from backend.agents.orchestrator import (
    supervisor_node,
    validate_and_complete_plan,
    run_agent_loop,
    run_agent_loop_stream,
    merge_worker_results,
    make_initial_state,
    _build_supervisor_prompt,
)

logger = logging.getLogger("graph")


def build_graph():
    """No-op stub — the graph is no longer built.

    If code calls this expecting a compiled LangGraph graph, it will get
    ``None``.  All execution paths have been migrated to use
    ``run_agent_loop`` / ``run_agent_loop_stream`` directly.
    """
    logger.debug("build_graph() called — Agent Loop mode, no graph built")
    return None


def get_multi_agent_graph():
    """No-op stub — returns ``None``.

    Callers should use ``run_agent_loop()`` or ``run_agent_loop_stream()``
    from ``backend.agents.orchestrator`` instead.
    """
    return None


def rebuild_graph():
    """No-op stub — graph rebuild is unnecessary in Agent Loop mode."""
    pass
