"""Agent Loop Orchestrator — Claude Code-style sequential wave execution.

Replaces LangGraph's StateGraph / Send / conditional edges with an explicit
async loop.  Workers are grouped into "waves": all agents whose dependencies
are satisfied run in parallel via ``asyncio.gather``.  After each wave,
results are merged into the state, and the next wave is computed.

This is deterministic, debuggable, and does not rely on LangGraph's graph
topology for correctness.

Safety layer (``backend.safety``) wraps the loop at four checkpoints:

1. **Plan review** — before wave 1: unknown agents are dropped, budgets are
   checked, and the plan is screened for destructive/exfiltrating intent.
2. **Action gate** — before each task: high-risk actions stop for a human
   approval (streaming) or the configured non-streaming fallback.
3. **Output review** — after each task: credentials are redacted before the
   result enters state, and errors/empty results are recorded.
4. **Goal confirmation** — after the last wave: the artifacts are checked
   against the user's request; unmet items feed a bounded remediation round.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, AsyncGenerator, Callable, Optional

from langchain_core.messages import HumanMessage

from backend.models.provider import create_chat_model
from backend.cache import cache
from backend.skills.registry import skill_registry
from backend.agents import recovery
from backend.agents.builtin_workers import synthesizer_node
from backend.prompts import SUPERVISOR_PROMPT_TEMPLATE
from backend.observability import RunChain, current_trace
from backend.observability.chain import (
    KIND_ATTEMPT,
    KIND_BUDGET,
    KIND_MEMORY,
    KIND_PLAN,
    KIND_REMEDIATE,
    KIND_REVIEW,
    KIND_RUN,
    KIND_TASK,
    KIND_VERIFY,
)
from backend.safety import RunBudget, SafetyReviewer, new_run_id
from backend.safety.policy import PolicyDecision

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
    session_id: str = "",
    run_id: str = "",
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
        "session_id": session_id,
        "run_id": run_id or new_run_id(),
        # Safety bookkeeping
        "safety_blocks": [],
        "dropped_tasks": [],
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
        if key == "research_results":
            sources = sum(
                len(item.get("sources") or []) for item in items if isinstance(item, dict)
            )
            rounds = max(
                (item.get("research_hops") or 1 for item in items if isinstance(item, dict)),
                default=1,
            )
            gaps = sum(
                len(item.get("knowledge_gaps") or []) for item in items if isinstance(item, dict)
            )
            summary = f"{rounds} 轮检索 · {sources} 个来源"
            if gaps:
                summary += f" · {gaps} 处信息缺口"
            return {"result_key": key, "summary": summary, "count": len(items),
                    "sources": sources, "rounds": rounds}
        return {
            "result_key": key,
            "summary": f"completed {agent}: {len(items)} result(s)",
            "count": len(items),
        }
    return {"result_key": "", "summary": f"completed {agent}", "count": 1}


# ── Supervisor ─────────────────────────────────────────────────────────

def _build_supervisor_prompt(memories: str = "") -> str:
    skills_section = skill_registry.build_skills_section()
    routing_rules = skill_registry.build_routing_rules()
    prompt = SUPERVISOR_PROMPT_TEMPLATE.replace("{skills_section}", skills_section)
    prompt = prompt.replace("{routing_rules}", routing_rules)
    if memories:
        prompt = f"{memories}\n\n{prompt}"
    return prompt


async def supervisor_node(state: dict) -> dict:
    """Plan Mode supervisor — generates a step-by-step execution plan."""
    user_req = state["user_request"]
    cache_key = f"route:{cache.hash_key(user_req)}"

    router_id = state.get("router_model_id", "") or state.get("chat_model_id", "")
    llm = create_chat_model(
        model_id=router_id or "deepseek-ai/DeepSeek-V3",
        temperature=0.1, max_tokens=2048, provider_config=None,
    )

    prompt = _build_supervisor_prompt(state.get("_memories") or "").replace(
        "{user_request}", user_req
    )
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


# ── Safety helpers ─────────────────────────────────────────────────────

def _refusal_text(decision: PolicyDecision, note: str = "") -> str:
    """User-facing explanation when safety review stops a run."""
    lines = [f"⛔ **执行审查拦截**：{decision.summary or '该请求未通过安全策略'}"]
    for reason in decision.reasons[:5]:
        lines.append(f"- {reason}")
    if note:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("如确认是误判，可调整请求后重试，或在「🔒 安全」选项卡中调整审查策略。")
    return "\n".join(lines)


def _block_record(task: dict, decision: PolicyDecision, stage: str, note: str = "") -> dict:
    return {
        "task_id": task.get("id", ""),
        "agent": task.get("agent", ""),
        "stage": stage,
        "risk": decision.risk,
        "summary": decision.summary,
        "reasons": decision.reasons[:5],
        "note": note,
    }


def _budget_note(reviewer: SafetyReviewer) -> str:
    reasons = reviewer.budget.exceeded()
    return "；".join(reasons) if reasons else "执行预算已用尽"


def _chain():
    """Return the active run chain, or a chain that records nothing.

    Tracing is best-effort: a failure here must never affect a run.
    """
    trace = current_trace()
    return trace.chain if trace is not None else RunChain.disabled()


def _chain_status(state: dict) -> str:
    """Map a finished run onto a span status."""
    from backend.observability.chain import _BLOCKED, _FAILED, _OK, _RETRIED

    # A refusal by review is not a success — say so in the chain.
    if state.get("_refused"):
        return _BLOCKED
    verdict = (state.get("_verification") or {}).get("status", "")
    if verdict == "complete":
        return _OK
    if verdict in ("partial", "incomplete"):
        return _RETRIED
    if verdict == "failed":
        return _FAILED
    if state.get("safety_blocks") or state.get("_stopped_reason"):
        return _RETRIED
    return _OK


async def _propose_recovery(
    user_request: str,
    tasks: list[dict],
    state: dict,
    verdict: dict,
    reviewer: SafetyReviewer,
) -> tuple[list[dict], str, str]:
    """Decide how to recover from an incomplete run.

    An incremental LLM re-plan is preferred because it can route *around* a
    broken agent (different step, different decomposition); the mechanical
    remediation derived from the unmet criteria is the deterministic fallback.

    Returns ``(tasks, strategy, reason)``; the caller owns budget accounting.
    """
    from backend.safety import verifier as _verifier
    from backend.safety import get_safety_settings

    if not verdict or verdict.get("status") == _verifier.COMPLETE:
        return [], "none", ""
    # ``auto_remediate`` governs recovery as a whole, including the LLM
    # re-plan — otherwise switching it off would still spend a model call.
    if not get_safety_settings().auto_remediate:
        return [], "none", ""

    if recovery.llm_replan_enabled() and reviewer.budget.allow_llm_review():
        reviewer.budget.consume_llm_review()
        planned, reason = await recovery.replan(
            user_request=user_request,
            tasks=tasks,
            state=state,
            verdict=verdict,
            catalog=reviewer.catalog,
            model_id=reviewer.model_id,
        )
        if planned:
            return planned, "llm_replan", reason

    return reviewer.build_remediation(tasks), "mechanical", ""



async def run_agent_loop_stream(
    initial_state: dict,
    agent_labels: dict[str, str],
    agent_map: dict[str, str],
) -> AsyncGenerator[dict, None]:
    """Streaming entry point — drives the LangGraph backbone.

    ``agent_map`` is accepted for backward compatibility; the graph resolves
    workers from the skill registry directly.
    """
    from backend.agents.runner import stream_run

    async for event in stream_run(initial_state, agent_labels):
        yield event


async def run_agent_loop(initial_state: dict) -> dict:
    """Non-streaming entry point used by ``/api/chat/sync``.

    Runs the same graph, then applies the synthesizer unless the caller asked
    to skip it.
    """
    from backend.agents.runner import run_graph

    state = await run_graph(initial_state, {})

    if not state.get("_skip_synthesizer") and not state.get("final_response"):
        logger.info("Agent Loop: running synthesizer...")
        synth_result = await synthesizer_node(state)
        merge_worker_results(state, [synth_result])

    return state
