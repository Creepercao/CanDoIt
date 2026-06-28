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
import os
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
            if key == "_add_tasks":
                if not isinstance(value, list):
                    continue
                existing = state.get("tasks", [])
                state["tasks"] = _normalize_tasks(existing + value)
                continue
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


def _normalize_tasks(tasks: list[dict]) -> list[dict]:
    """Assign stable ids and remove exact duplicate dynamic tasks."""
    result: list[dict] = []
    seen: set[str] = set()
    counters: dict[str, int] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        agent = task.get("agent", "")
        if not agent:
            continue
        counters[agent] = counters.get(agent, 0) + 1
        if not task.get("id"):
            suffix = task.get("slide_index") or counters[agent]
            task = {**task, "id": f"{agent}:{suffix}"}
        dedupe_key = f"{task.get('id')}|{agent}|{task.get('prompt', '')}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        result.append(task)
    return result


def _task_deps(task: dict, tasks: list[dict]) -> set[str]:
    """Return task ids that must complete before *task* can run."""
    deps: set[str] = set()
    explicit = task.get("depends_on") or []
    for dep in explicit:
        if isinstance(dep, int):
            idx = dep - 1
            if 0 <= idx < len(tasks):
                deps.add(tasks[idx].get("id", ""))
        elif isinstance(dep, str):
            deps.add(dep)

    agent_deps = _agent_deps(task.get("agent", ""))
    for dep_agent in agent_deps:
        dep_tasks = [t for t in tasks if t.get("agent") == dep_agent]
        deps.update(t.get("id", "") for t in dep_tasks)
    return {d for d in deps if d}


def _state_for_task(state: dict, task: dict) -> dict:
    """Worker-compatible state copy containing only the current task."""
    task_state = dict(state)
    task_state["tasks"] = [task]
    task_state["_current_task"] = task
    return task_state


def _summarize_output(agent: str, output: dict) -> dict:
    for key in RESULT_KEYS:
        value = output.get(key)
        if not value:
            continue
        if key == "skill_outputs" and isinstance(value, dict):
            total = sum(len(items or []) for items in value.values())
            names = ", ".join(value.keys())
            return {
                "result_key": key,
                "summary": f"completed {agent}: {total} skill output(s) ({names})",
                "count": total,
            }
        items = value if isinstance(value, list) else [value]
        return {
            "result_key": key,
            "summary": f"completed {agent}: {len(items)} result(s)",
            "count": len(items),
        }
    return {"result_key": "", "summary": f"completed {agent}", "count": 1}


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

    ppt_keywords = (
        "ppt", "powerpoint", "presentation", "slide", "slides",
        "演示", "演示文稿", "幻灯片", "汇报", "做ppt", "生成ppt",
    )
    wants_ppt = any(k in user_request.lower() for k in ppt_keywords)

    # Prefer the parallel built-in PPT pipeline over the legacy monolithic
    # ppt-animation package skill.
    if wants_ppt:
        converted: list[dict] = []
        for task in tasks:
            if task.get("agent") == "ppt-animation":
                converted.append({
                    "agent": "ppt_planner",
                    "prompt": task.get("prompt") or f"Plan a slide deck for: {user_request}",
                })
            else:
                converted.append(task)
        tasks = converted

    agent_types = set(t.get("agent", "") for t in tasks)

    if wants_ppt and "ppt_planner" not in agent_types:
        tasks.append({
            "agent": "ppt_planner",
            "prompt": f"Plan a slide deck for: {user_request}",
        })
        agent_types.add("ppt_planner")

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
        if wants_ppt and skill.name == "ppt-animation":
            continue
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


def _task_timeout_seconds(agent_type: str) -> float:
    """Hard timeout for one scheduled task.

    Individual workers may use shorter internal timeouts to produce graceful
    fallbacks. This limit prevents a whole wave from waiting forever when a
    provider request never resolves.
    """
    specific = {
        "ppt_planner": os.environ.get("PPT_PLANNER_TIMEOUT_SECONDS", "240"),
        "ppt_slide": os.environ.get("PPT_SLIDE_TIMEOUT_SECONDS", "210"),
        "ppt_assembler": os.environ.get("PPT_ASSEMBLER_TIMEOUT_SECONDS", "120"),
        "research": os.environ.get("RESEARCH_TASK_TIMEOUT_SECONDS", "600"),
    }.get(agent_type)
    raw = specific or os.environ.get("AGENT_TASK_TIMEOUT_SECONDS", "600")
    try:
        return max(30.0, float(raw))
    except (TypeError, ValueError):
        return 600.0


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

    tasks = _normalize_tasks(state.get("tasks", []))
    if not tasks:
        state["final_response"] = "I didn't understand that request."
        return state

    # 2. Validate
    tasks = _normalize_tasks(validate_and_complete_plan(tasks, state.get("user_request", "")))
    state["tasks"] = tasks

    # 3. Wave execution
    workers = _get_worker_registry()
    completed: set[str] = set()

    while True:
        tasks = _normalize_tasks(state.get("tasks", []))
        state["tasks"] = tasks
        all_task_ids = {t.get("id", "") for t in tasks}
        if completed >= all_task_ids:
            break

        # Find tasks whose deps are all completed
        wave: list[tuple[dict, Callable]] = []
        for task in tasks:
            agent = task.get("agent", "")
            task_id = task.get("id", "")
            if task_id in completed:
                continue
            deps = _task_deps(task, tasks)
            if all(d in completed for d in deps):
                worker_fn = workers.get(agent)
                if worker_fn:
                    wave.append((task, worker_fn))

        if not wave:
            logger.warning(
                "Agent Loop: deadlock detected — "
                f"completed={completed}, pending={all_task_ids - completed}"
            )
            break

        logger.info(
            f"Agent Loop: wave {[t.get('id') for t, _ in wave]} "
            f"({len(wave)} task(s))"
        )

        # Execute wave in parallel
        async def _run_one(task: dict, fn: Callable, st: dict) -> tuple[str, dict]:
            agent = task.get("agent", "")
            try:
                timeout = _task_timeout_seconds(agent)
                result = await asyncio.wait_for(fn(_state_for_task(st, task)), timeout=timeout)
                logger.info(f"Agent Loop: '{task.get('id')}' completed")
                return task.get("id", ""), result
            except asyncio.TimeoutError:
                logger.error(
                    "Agent Loop: '%s' timed out after %.0fs",
                    task.get("id"),
                    _task_timeout_seconds(agent),
                )
                return task.get("id", ""), {f"{agent}_results": [{"error": "task timed out"}]}
            except Exception as e:
                logger.error(f"Agent Loop: '{task.get('id')}' error: {e}")
                return task.get("id", ""), {f"{agent}_results": [{"error": str(e)}]}

        results = await asyncio.gather(*[
            _run_one(task, fn, state) for task, fn in wave
        ])

        # Merge results
        merge_worker_results(state, [output for _task_id, output in results])
        for task_id, _output in results:
            if task_id:
                completed.add(task_id)

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

    tasks = _normalize_tasks(state.get("tasks", []))
    plan_steps = state.get("plan_steps", [])

    if not tasks:
        yield {"event": "final", "data": {"response": "unable to understand request"}}
        yield {"event": "done", "data": {}}
        return

    # 2. Validate & emit plan
    tasks = _normalize_tasks(validate_and_complete_plan(tasks, state.get("user_request", "")))
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
    collected_html: list[dict] = []
    accumulated: dict[str, list] = {}

    while True:
        tasks = _normalize_tasks(state.get("tasks", []))
        state["tasks"] = tasks
        all_task_ids = {t.get("id", "") for t in tasks}
        if completed >= all_task_ids:
            break

        wave: list[tuple[dict, Callable]] = []
        for task in tasks:
            agent = task.get("agent", "")
            task_id = task.get("id", "")
            if task_id in completed:
                continue
            deps = _task_deps(task, tasks)
            if all(d in completed for d in deps):
                worker_fn = workers.get(agent)
                if worker_fn:
                    wave.append((task, worker_fn))

        if not wave:
            logger.warning(
                f"Agent Loop: deadlock — completed={completed}, "
                f"pending={all_task_ids - completed}"
            )
            break

        logger.info(
            f"Agent Loop stream: wave {[t.get('id') for t, _ in wave]}"
        )

        # Emit agent_start for each task in this wave
        for task, _ in wave:
            agent = task.get("agent", "")
            node_name = agent_map.get(agent, f"{agent}_worker")
            yield {"event": "agent_start", "data": {
                "agent": task.get("id") or node_name,
                "agent_type": agent,
                "label": agent_labels.get(node_name, agent),
                "task": task.get("prompt", "")[:180],
                "task_count": 1,
                "task_id": task.get("id", ""),
                "slide_index": task.get("slide_index"),
            }}

        # Execute wave in parallel, with heartbeat to prevent timeout
        async def _run_one(task: dict, fn: Callable, st: dict) -> tuple[dict, dict]:
            agent = task.get("agent", "")
            try:
                timeout = _task_timeout_seconds(agent)
                result = await asyncio.wait_for(fn(_state_for_task(st, task)), timeout=timeout)
                return task, result
            except asyncio.TimeoutError:
                logger.error(
                    "Agent Loop: '%s' timed out after %.0fs",
                    task.get("id"),
                    _task_timeout_seconds(agent),
                )
                return task, {f"{agent}_results": [{"error": "task timed out"}]}
            except Exception as e:
                logger.error(f"Agent Loop: '{task.get('id')}' error: {e}")
                return task, {f"{agent}_results": [{"error": str(e)}]}

        wave_tasks = [
            _run_one(task, fn, state) for task, fn in wave
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
        for task, output in results:
            agent = task.get("agent", "")
            merge_worker_results(state, [output])
            completed.add(task.get("id", ""))

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
                                    if item.get("html_url"):
                                        collected_html.append({
                                            "skill_name": sk_name,
                                            "html_url": item["html_url"],
                                            "title": item.get("html_title", ""),
                                            "task": item.get("task", ""),
                                        })
                                        continue
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
            summary = _summarize_output(agent, output)
            yield {"event": "agent_done", "data": {
                "agent": task.get("id") or node_name,
                "agent_type": agent,
                "label": agent_labels.get(node_name, agent),
                "task_id": task.get("id", ""),
                "slide_index": task.get("slide_index"),
                **summary,
            }}

    yield {"event": "phase", "data": {"phase": "synthesize", "message": "synthesizing..."}}

    # Pass accumulated state + HTML to caller for synthesizer streaming
    yield {"event": "_agent_loop_done", "data": {
        "state": state,
        "accumulated": accumulated,
        "collected_html": collected_html,
    }}
