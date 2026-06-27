"""Agent Loop Orchestrator — Claude Code-style sequential wave execution.

Replaces LangGraph's StateGraph / Send / conditional edges with an explicit
async loop.  Workers are grouped into "waves": all agents whose dependencies
are satisfied run in parallel via ``asyncio.gather``.  After each wave,
results are merged into the state, and the next wave is computed.

This is deterministic, debuggable, and does not rely on LangGraph's graph
topology for correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncGenerator, Callable, Optional

from langchain_core.messages import HumanMessage

from backend.models.provider import create_chat_model
from backend.cache import cache
from backend.skills.registry import skill_registry
from backend.agents.builtin_workers import synthesizer_node
from backend.prompts import SUPERVISOR_PROMPT_TEMPLATE

logger = logging.getLogger("orchestrator")


# ── State helpers ──────────────────────────────────────────────────────

RESULT_KEYS = (
    "research_results", "analyst_results", "chart_results",
    "image_results", "video_results", "code_results",
    "skill_outputs",
)

# Result keys that use list-append semantics (multiple workers may contribute)
_LIST_RESULT_KEYS = frozenset({
    "research_results", "analyst_results", "chart_results",
    "image_results", "video_results", "code_results",
    "messages",
})


def make_initial_state(
    user_request: str = "",
    chat_model_id: str = "",
    image_model_id: str = "",
    video_model_id: str = "",
    router_model_id: str = "",
    history: list[dict] = None,
) -> dict:
    """Create a fresh state dict for a new request."""
    return {
        "messages": list(history or []),
        "user_request": user_request,
        "tasks": [],
        "plan_steps": [],
        "research_results": [],
        "analyst_results": [],
        "chart_results": [],
        "image_results": [],
        "video_results": [],
        "code_results": [],
        "skill_outputs": {},
        "final_response": "",
        "chat_model_id": chat_model_id,
        "image_model_id": image_model_id,
        "video_model_id": video_model_id,
        "router_model_id": router_model_id,
        "_skip_synthesizer": False,
    }


def merge_worker_results(state: dict, outputs: list[dict]) -> dict:
    """Merge worker return dicts into *state* (in-place), replicating
    the ``Annotated[..., reducer]`` semantics from LangGraph.

    - ``*_results`` keys → append to list
    - ``skill_outputs`` → merge dict-of-lists
    - ``messages`` → append
    - everything else → overwrite
    """
    for output in outputs:
        if not isinstance(output, dict):
            continue
        for key, value in output.items():
            if key in _LIST_RESULT_KEYS:
                existing = state.get(key, [])
                if isinstance(existing, list) and isinstance(value, list):
                    state[key] = existing + value
                elif isinstance(value, list):
                    state[key] = list(value)
            elif key == "skill_outputs":
                existing = state.get(key, {}) or {}
                if isinstance(value, dict):
                    for sk_name, sk_items in value.items():
                        if sk_name in existing:
                            existing[sk_name] = existing[sk_name] + list(sk_items or [])
                        else:
                            existing[sk_name] = list(sk_items or [])
                    state[key] = existing
            else:
                state[key] = value
    return state


# ── Supervisor ─────────────────────────────────────────────────────────

def _build_supervisor_prompt() -> str:
    skills_section = skill_registry.build_skills_section()
    routing_rules = skill_registry.build_routing_rules()
    prompt = SUPERVISOR_PROMPT_TEMPLATE.replace("{skills_section}", skills_section)
    return prompt.replace("{routing_rules}", routing_rules)


async def supervisor_node(state: dict) -> dict:
    """Plan Mode supervisor — generates a step-by-step execution plan."""
    user_req = state["user_request"]
    cache_key = f"route:{cache.hash_key(user_req)}"

    router_id = state.get("router_model_id", "") or state.get("chat_model_id", "")
    llm = create_chat_model(
        model_id=router_id or "deepseek-ai/DeepSeek-V3",
        temperature=0.1, max_tokens=2048, provider_config=None,
    )

    prompt = _build_supervisor_prompt().replace("{user_request}", user_req)
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    content = response.content if hasattr(response, "content") else str(response)

    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    tasks: list[dict] = []
    plan_steps: list[dict] = []
    direct = ""
    if json_match:
        try:
            data = json.loads(json_match.group())
            plan = data.get("plan", [])
            if plan:
                for step in plan:
                    tasks.append({
                        "agent": step.get("agent", ""),
                        "prompt": step.get("prompt", ""),
                    })
                    plan_steps.append({
                        "step": step.get("step", len(plan_steps) + 1),
                        "agent": step.get("agent", ""),
                        "prompt": step.get("prompt", ""),
                        "depends_on": step.get("depends_on", []),
                        "reason": step.get("reason", ""),
                    })
                logger.info(
                    f"Plan Mode: parsed {len(plan_steps)} steps → "
                    f"agents: {[s['agent'] for s in plan_steps]}"
                )
            else:
                tasks = data.get("tasks", [])
                for i, t in enumerate(tasks):
                    plan_steps.append({
                        "step": i + 1,
                        "agent": t.get("agent", ""),
                        "prompt": t.get("prompt", ""),
                        "depends_on": [],
                        "reason": "",
                    })
            direct = data.get("direct_response", "")
        except json.JSONDecodeError:
            direct = content

    if direct and not tasks:
        return {"tasks": [], "plan_steps": [], "final_response": direct}
    if tasks:
        await cache.set(cache_key, tasks, ttl=300)
        return {"tasks": tasks, "plan_steps": plan_steps}
    return {
        "tasks": [],
        "plan_steps": [],
        "final_response": content[:1000] or "I didn't understand that. Could you rephrase?",
    }


# ── Plan validation & auto-completion ──────────────────────────────────

def validate_and_complete_plan(tasks: list[dict], user_request: str) -> list[dict]:
    """Validate and auto-complete a task plan.

    Returns a (possibly augmented) task list.  Handles:
    1. Upstream dependency injection (missing research → auto-add)
    2. Downstream skill suggestion (user asks for PPT → auto-add ppt-animation)
    """
    if not tasks:
        return tasks

    agent_types = set(t.get("agent", "") for t in tasks)

    # ── 1. Auto-inject missing upstream dependencies ──
    for agent_type in list(agent_types):
        skill = skill_registry.get(agent_type)
        if not skill or not skill.depends_on:
            continue
        for dep in skill.depends_on:
            if dep not in agent_types:
                dep_task = next(
                    (t for t in tasks if t.get("agent") == agent_type), {}
                )
                dep_prompt = dep_task.get("prompt", "")
                tasks.insert(0, {
                    "agent": dep,
                    "prompt": f"Search and gather factual data for: {dep_prompt or agent_type}",
                })
                agent_types.add(dep)
                logger.info(
                    f"Auto-injected '{dep}' task (required by '{agent_type}' — "
                    f"supervisor omitted it)"
                )

    # ── 2. Auto-suggest downstream skills ──
    agent_types = set(t.get("agent", "") for t in tasks)
    user_req_lower = user_request.lower()
    for skill in skill_registry.get_enabled().values():
        if not skill.depends_on:
            continue
        if skill.name in agent_types:
            continue
        if not skill.worker:
            continue

        deps_satisfied = all(d in agent_types for d in skill.depends_on)
        if not deps_satisfied:
            continue

        should_inject = False
        if skill._package_meta:
            triggers = skill._package_meta.get("triggers", [])
            if isinstance(triggers, list):
                for trigger in triggers:
                    if trigger.lower() in user_req_lower:
                        should_inject = True
                        break

        if not should_inject:
            continue

        tasks.append({
            "agent": skill.name,
            "prompt": (
                f"基于 {', '.join(skill.depends_on)} 的搜集结果，"
                f"生成 {skill.display_name} 的完整输出。"
                f"用户原始请求: {user_request[:300]}"
            ),
        })
        agent_types.add(skill.name)
        logger.info(
            f"Auto-injected '{skill.name}' task (depends on "
            f"{skill.depends_on}, user request matched trigger)"
        )

    return tasks


# ── Worker registry ────────────────────────────────────────────────────

def _get_worker_registry() -> dict[str, Callable]:
    """Return ``{agent_type: worker_function}`` for all enabled agents."""
    workers: dict[str, Callable] = {}
    for skill in skill_registry.get_enabled().values():
        if skill.worker is not None:
            workers[skill.name] = skill.worker
    return workers


def _agent_deps(agent_type: str) -> list[str]:
    """Return the dependency list for *agent_type*."""
    skill = skill_registry.get(agent_type)
    return list(skill.depends_on) if skill else []


# ── Agent Loop (non-streaming) ─────────────────────────────────────────

async def run_agent_loop(initial_state: dict) -> dict:
    """Execute the full agent pipeline synchronously.

    1. Supervisor → plan
    2. Validate & complete plan
    3. Wave execution loop
    4. Synthesizer
    5. Return final state
    """
    state = dict(initial_state)

    # 1. Supervisor
    logger.info("Agent Loop: running supervisor...")
    sup_result = await supervisor_node(state)
    merge_worker_results(state, [sup_result])

    if state.get("final_response"):
        return state

    tasks = state.get("tasks", [])
    if not tasks:
        state["final_response"] = "I didn't understand that request."
        return state

    # 2. Validate
    tasks = validate_and_complete_plan(tasks, state.get("user_request", ""))
    state["tasks"] = tasks

    # 3. Wave execution
    workers = _get_worker_registry()
    completed = set()
    all_agents = set(t.get("agent", "") for t in tasks)

    while completed != all_agents:
        # Find agents whose deps are all completed
        wave: list[tuple[str, dict]] = []
        for task in tasks:
            agent = task.get("agent", "")
            if agent in completed:
                continue
            deps = _agent_deps(agent)
            if all(d in completed for d in deps):
                worker_fn = workers.get(agent)
                if worker_fn:
                    wave.append((agent, worker_fn))

        if not wave:
            logger.warning(
                f"Agent Loop: deadlock detected — "
                f"completed={completed}, pending={all_agents - completed}"
            )
            break

        logger.info(
            f"Agent Loop: wave {sorted(a for a, _ in wave)} "
            f"({len(wave)} agents)"
        )

        # Execute wave in parallel
        async def _run_one(agent: str, fn: Callable, st: dict) -> dict:
            try:
                result = await fn(st)
                logger.info(f"Agent Loop: '{agent}' completed")
                return result
            except Exception as e:
                logger.error(f"Agent Loop: '{agent}' error: {e}")
                return {f"{agent}_results": [{"error": str(e)}]}

        results = await asyncio.gather(*[
            _run_one(agent, fn, state) for agent, fn in wave
        ])

        # Merge results
        merge_worker_results(state, results)
        for agent, _ in wave:
            completed.add(agent)

    # 4. Synthesizer
    logger.info("Agent Loop: running synthesizer...")
    if not state.get("_skip_synthesizer"):
        synth_result = await synthesizer_node(state)
        merge_worker_results(state, [synth_result])

    return state


# ── Agent Loop (streaming) ─────────────────────────────────────────────

async def run_agent_loop_stream(
    initial_state: dict,
    agent_labels: dict[str, str],
    agent_map: dict[str, str],
) -> AsyncGenerator[dict, None]:
    """Execute the agent pipeline, yielding lifecycle events.

    Yields dicts with keys ``event`` and ``data``, compatible with the
    existing SSE streaming layer in ``chat.py``.
    """
    state = dict(initial_state)

    # 1. Supervisor
    yield {"event": "phase", "data": {"phase": "supervisor", "message": "analyzing request..."}}
    sup_result = await supervisor_node(state)
    merge_worker_results(state, [sup_result])

    if state.get("final_response"):
        yield {"event": "phase", "data": {"phase": "done", "message": "direct reply"}}
        yield {"event": "final", "data": {"response": state["final_response"]}}
        yield {"event": "done", "data": {}}
        return

    tasks = state.get("tasks", [])
    plan_steps = state.get("plan_steps", [])

    if not tasks:
        yield {"event": "final", "data": {"response": "unable to understand request"}}
        yield {"event": "done", "data": {}}
        return

    # 2. Validate & emit plan
    tasks = validate_and_complete_plan(tasks, state.get("user_request", ""))
    state["tasks"] = tasks

    yield {"event": "plan", "data": {
        "tasks": [
            {
                "agent": t.get("agent", ""),
                "label": agent_labels.get(agent_map.get(t.get("agent", ""), ""), t.get("agent", "")),
                "prompt": t.get("prompt", "")[:120],
            }
            for t in tasks
        ],
        "count": len(tasks),
        "steps": [
            {
                "step": s.get("step", i + 1),
                "agent": s.get("agent", ""),
                "label": agent_labels.get(agent_map.get(s.get("agent", ""), ""), s.get("agent", "")),
                "prompt": s.get("prompt", "")[:120],
                "depends_on": s.get("depends_on", []),
                "reason": s.get("reason", ""),
            }
            for i, s in enumerate(plan_steps)
        ] if plan_steps else [],
    }}

    # 3. Wave execution
    workers = _get_worker_registry()
    completed: set[str] = set()
    all_agents = set(t.get("agent", "") for t in tasks)
    emitted: set[str] = set()
    collected_html: list[dict] = []
    accumulated: dict[str, list] = {}

    while completed != all_agents:
        wave: list[tuple[str, Callable]] = []
        for task in tasks:
            agent = task.get("agent", "")
            if agent in completed:
                continue
            deps = _agent_deps(agent)
            if all(d in completed for d in deps):
                worker_fn = workers.get(agent)
                if worker_fn:
                    wave.append((agent, worker_fn))

        if not wave:
            logger.warning(
                f"Agent Loop: deadlock — completed={completed}, "
                f"pending={all_agents - completed}"
            )
            break

        logger.info(
            f"Agent Loop stream: wave {sorted(a for a, _ in wave)}"
        )

        # Emit agent_start for each worker in this wave
        for agent, _ in wave:
            node_name = agent_map.get(agent, f"{agent}_worker")
            if node_name not in emitted:
                emitted.add(node_name)
                node_tasks = [t for t in tasks if t.get("agent") == agent]
                yield {"event": "agent_start", "data": {
                    "agent": node_name,
                    "agent_type": agent,
                    "label": agent_labels.get(node_name, agent),
                    "task": ",".join(t.get("prompt", "") for t in node_tasks)[:180],
                    "task_count": len(node_tasks),
                }}

        # Execute wave in parallel, with heartbeat to prevent timeout
        async def _run_one(agent: str, fn: Callable, st: dict) -> tuple[str, dict]:
            try:
                result = await fn(st)
                return agent, result
            except Exception as e:
                logger.error(f"Agent Loop: '{agent}' error: {e}")
                return agent, {f"{agent}_results": [{"error": str(e)}]}

        wave_tasks = [
            _run_one(agent, fn, state) for agent, fn in wave
        ]
        # Wrap in a task so we can heartbeat while waiting
        wave_future = asyncio.gather(*wave_tasks)

        # Heartbeat loop: yield a comment every 30s to keep SSE alive
        while True:
            done, _ = await asyncio.wait(
                [wave_future], timeout=30.0
            )
            if done:
                break
            yield {"event": "heartbeat", "data": {}}

        results = await wave_future

        # Merge and emit agent_done
        for agent, output in results:
            merge_worker_results(state, [output])
            completed.add(agent)

            # Accumulate for final event
            for key in RESULT_KEYS:
                if key in output and output[key]:
                    accumulated.setdefault(key, []).extend(
                        output[key] if isinstance(output[key], list) else [output[key]]
                    )
                    if key == "skill_outputs" and isinstance(output[key], dict):
                        from backend.api import _HTML_DETECT_RE, save_skill_html as _save_html
                        for sk_name, sk_items in output[key].items():
                            for item in (sk_items or []):
                                if isinstance(item, dict):
                                    result_text = item.get("result", "")
                                    if isinstance(result_text, str) and _HTML_DETECT_RE.search(result_text[:500]):
                                        saved = _save_html(sk_name, result_text, title=item.get("task", ""))
                                        item["html_url"] = saved["html_url"]
                                        item["html_title"] = saved["title"]
                                        collected_html.append({
                                            "skill_name": sk_name,
                                            "html_url": item["html_url"],
                                            "title": item.get("html_title", ""),
                                            "task": item.get("task", ""),
                                        })

            # Summary
            node_name = agent_map.get(agent, f"{agent}_worker")
            yield {"event": "agent_done", "data": {
                "agent": node_name,
                "agent_type": agent,
                "label": agent_labels.get(node_name, agent),
                "summary": f"completed {agent}",
                "count": 1,
                "result_key": f"{agent}_results" if agent != "skill" else "skill_outputs",
            }}

    yield {"event": "phase", "data": {"phase": "synthesize", "message": "synthesizing..."}}

    # Pass accumulated state + HTML to caller for synthesizer streaming
    yield {"event": "_agent_loop_done", "data": {
        "state": state,
        "accumulated": accumulated,
        "collected_html": collected_html,
    }}
