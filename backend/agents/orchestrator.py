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


# ── Agent Loop (shared core) ───────────────────────────────────────────

async def _run_agent_loop_events(
    initial_state: dict,
    agent_labels: dict[str, str],
    agent_map: dict[str, str],
    *,
    streaming: bool,
) -> AsyncGenerator[dict, None]:
    """Run the whole agent pipeline, yielding lifecycle events.

    This is the **single** orchestration core: ``run_agent_loop_stream``
    forwards every event to SSE, while ``run_agent_loop`` drains the generator
    and keeps only the final state.  Both modes share the wave scheduling, the
    safety checkpoints, the budget guard and the goal-confirmation round, so a
    behaviour fix only has to be written once.

    The one intentional difference is how a ``review`` decision is resolved:

    * ``streaming=True``  — emit ``approval_required`` and wait for a human.
    * ``streaming=False`` — apply ``SAFETY_NONSTREAM_REVIEW`` immediately.

    Events yielded: ``phase``, ``plan``, ``agent_start``, ``agent_done``,
    ``heartbeat``, ``safety_review``, ``approval_required``,
    ``approval_resolved``, ``verification``, ``remediation``,
    ``budget_exceeded``, ``final``, ``done`` and the terminal
    ``_agent_loop_done`` carrying the final state.
    """
    state = dict(initial_state)
    reviewer = SafetyReviewer(
        state.get("run_id") or new_run_id(),
        session_id=state.get("session_id", ""),
        streaming=streaming,
        budget=RunBudget.from_env(),
        model_id=state.get("chat_model_id", ""),
    )
    state["run_id"] = reviewer.run_id
    reviewer.record(
        "run.start", stage="start", message="开始执行",
        details={"request": (state.get("user_request") or "")[:500]},
    )

    # ── event builders ─────────────────────────────────────────────────

    def safety_review_event(decision: PolicyDecision, gate: str) -> dict:
        return {"event": "safety_review", "data": {
            "run_id": reviewer.run_id,
            "gate": gate,
            **decision.to_dict(),
        }}

    def finish_event(response: str, extra: dict = None) -> dict:
        state["final_response"] = response
        reviewer.record(
            "run.finish", stage="finish", severity="info", message="执行结束",
            details={
                "status": (state.get("_verification") or {}).get("status", ""),
                "blocks": len(state.get("safety_blocks") or []),
                "response_chars": len(response or ""),
            },
        )
        data = {
            "response": response,
            "run_id": reviewer.run_id,
            "safety": reviewer.summary(),
        }
        data.update(extra or {})
        return {"event": "final", "data": data}

    def state_event() -> dict:
        # Attach the summary on every exit path (refusals included) so the
        # non-streaming caller always finds it.
        state["_safety"] = reviewer.summary()
        return {"event": "_agent_loop_done", "data": {
            "state": state,
            "accumulated": accumulated,
            "collected_html": collected_html,
            "run_id": reviewer.run_id,
            "safety": state["_safety"],
            "dropped_tasks": dropped,
            "safety_blocks": state.get("safety_blocks") or [],
            "stopped_reason": stopped_reason,
        }}

    async def resolve_gate(decision: PolicyDecision, *, stage: str,
                           task: dict = None, target: str = ""):
        """Resolve a plan/action review decision.

        Returns ``(events, allowed, note)``; the caller yields *events* so the
        approval round-trip stays visible in the stream.
        """
        events: list[dict] = []
        if decision.blocked:
            return events, False, "被安全策略拦截"
        if not decision.requires_approval:
            return events, True, ""
        if streaming:
            request = await reviewer.request_approval(
                decision, stage=stage, target=target, task=task
            )
            events.append({"event": "approval_required", "data": request.to_dict()})
            resolved = await reviewer.wait_approval(request)
            events.append({"event": "approval_resolved", "data": resolved.to_dict()})
        else:
            resolved = await reviewer.resolve_without_human(
                decision, stage=stage, target=target
            )
        if resolved.status != "approved":
            return events, False, f"审批未通过（{resolved.decided_by}）"
        return events, True, ""

    accumulated: dict[str, list] = {}
    collected_html: list[dict] = []
    dropped: list[dict] = []
    stopped_reason = ""

    # ── 1. Supervisor ──────────────────────────────────────────────────

    yield {"event": "phase", "data": {"phase": "supervisor", "message": "analyzing request..."}}
    sup_result = await supervisor_node(state)
    merge_worker_results(state, [sup_result])

    if state.get("final_response"):
        yield {"event": "phase", "data": {"phase": "done", "message": "direct reply"}}
        yield finish_event(state["final_response"])
        yield {"event": "done", "data": {}}
        yield state_event()
        return

    tasks = _normalize_tasks(state.get("tasks", []))
    plan_steps = state.get("plan_steps", [])

    if not tasks:
        yield finish_event("unable to understand request")
        yield {"event": "done", "data": {}}
        yield state_event()
        return

    user_request = state.get("user_request", "")

    # ── 2. Validate, screen & publish the plan ─────────────────────────

    tasks = _normalize_tasks(validate_and_complete_plan(tasks, user_request))
    tasks, dropped = reviewer.screen_tasks(tasks)
    state["dropped_tasks"] = dropped
    state["tasks"] = tasks

    yield {"event": "plan", "data": {
        "run_id": reviewer.run_id,
        "tasks": [
            {
                "agent": t.get("agent", ""),
                "label": agent_labels.get(agent_map.get(t.get("agent", ""), ""), t.get("agent", "")),
                "prompt": t.get("prompt", "")[:120],
            }
            for t in tasks
        ],
        "count": len(tasks),
        "dropped": dropped,
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

    if not tasks:
        yield finish_event(
            "计划中的步骤都引用了当前未注册的代理，无法执行。请检查技能是否已启用。"
        )
        yield {"event": "done", "data": {}}
        yield state_event()
        return

    # ── 3. Safety checkpoint 1: plan review ────────────────────────────

    yield {"event": "phase", "data": {"phase": "review", "message": "执行审查中..."}}
    plan_decision = reviewer.review_plan(user_request, tasks)
    plan_decision = await reviewer.enrich_plan_review(user_request, tasks, plan_decision)
    reviewer.record_decision(plan_decision, target="plan")
    yield safety_review_event(plan_decision, "plan")

    gate_events, allowed, note = await resolve_gate(
        plan_decision, stage="plan", target="plan"
    )
    for event in gate_events:
        yield event
    if not allowed:
        yield finish_event(_refusal_text(plan_decision, note if note != "被安全策略拦截" else ""))
        yield {"event": "done", "data": {}}
        yield state_event()
        return

    # ── 4. Wave execution (+ 5. goal confirmation / remediation) ───────

    workers = _get_worker_registry()
    completed: set[str] = set()
    budget_exhausted = False

    while True:
        # ── inner wave loop ──
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
                stopped_reason = stopped_reason or "检测到任务依赖死锁，提前结束执行"
                break

            if not reviewer.budget.allow_wave() or not reviewer.budget.allow_steps(len(wave)):
                stopped_reason = _budget_note(reviewer)
                budget_exhausted = True
                logger.warning("Agent Loop: %s", stopped_reason)
                reviewer.record(
                    "guard.budget_exceeded", stage="wave", decision="block", risk="medium",
                    severity="warning", message=stopped_reason,
                    details=reviewer.budget.snapshot(),
                )
                yield {"event": "budget_exceeded", "data": {
                    "run_id": reviewer.run_id,
                    "reason": stopped_reason,
                    "budget": reviewer.budget.snapshot(),
                }}
                break

            # ── Safety checkpoint 2: per-task action gate ──
            approved_wave: list[tuple[dict, Callable]] = []
            for task, fn in wave:
                decision = reviewer.review_action(task)
                reviewer.record_decision(decision, target=task.get("id", ""))
                if decision.findings or decision.decision != "allow":
                    yield safety_review_event(decision, "action")

                gate_events, allowed, note = await resolve_gate(
                    decision, stage="action", task=task, target=task.get("id", "")
                )
                for event in gate_events:
                    yield event

                if not allowed:
                    agent = task.get("agent", "")
                    node_name = agent_map.get(agent, f"{agent}_worker")
                    state.setdefault("safety_blocks", []).append(
                        _block_record(task, decision, "action", note)
                    )
                    completed.add(task.get("id", ""))
                    yield {"event": "agent_start", "data": {
                        "agent": task.get("id") or node_name,
                        "agent_type": agent,
                        "label": agent_labels.get(node_name, agent),
                        "task": task.get("prompt", "")[:180],
                        "task_count": 1,
                        "task_id": task.get("id", ""),
                    }}
                    yield {"event": "agent_done", "data": {
                        "agent": task.get("id") or node_name,
                        "agent_type": agent,
                        "label": agent_labels.get(node_name, agent),
                        "task_id": task.get("id", ""),
                        "status": "blocked",
                        "result_key": "",
                        "count": 0,
                        "summary": "已拦截：" + (decision.summary or "未通过安全审查"),
                    }}
                    continue
                approved_wave.append((task, fn))

            if not approved_wave:
                continue

            reviewer.budget.consume_wave()
            reviewer.budget.consume_steps(len(approved_wave))
            logger.info(
                f"Agent Loop: wave {[t.get('id') for t, _ in approved_wave]}"
            )

            for task, _ in approved_wave:
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

            # Execute the wave in parallel, heart-beating while we wait.
            async def _run_one(task: dict, fn: Callable, st: dict) -> tuple[dict, dict, int, list[dict], float]:
                """Run one task, retrying while the result stays unusable.

                Returns ``(task, output, attempts, retry_events, duration)``.
                Transient provider failures (429 / timeout / connection) are
                retried unchanged after a backoff; semantic failures (empty or
                errored output) are retried with the reason fed back into the
                prompt.  Only the final attempt's output is merged into the
                state.
                """
                agent = task.get("agent", "")
                # Tag model calls made by this task so usage is attributed to it.
                from backend.observability import set_current_agent

                set_current_agent(agent)

                attempts = 0
                retry_events: list[dict] = []
                output: dict = {}
                current = task
                started = time.monotonic()

                while True:
                    attempts += 1
                    try:
                        timeout = _task_timeout_seconds(agent)
                        output = await asyncio.wait_for(
                            fn(_state_for_task(st, current)), timeout=timeout
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "Agent Loop: '%s' timed out after %.0fs",
                            task.get("id"),
                            _task_timeout_seconds(agent),
                        )
                        output = {f"{agent}_results": [{"error": "task timed out"}]}
                    except Exception as e:
                        logger.error(f"Agent Loop: '{task.get('id')}' error: {e}")
                        output = {f"{agent}_results": [{"error": str(e)}]}

                    if not isinstance(output, dict):
                        output = {f"{agent}_results": [{"error": "worker returned a non-dict result"}]}

                    if not recovery.is_unusable(output):
                        break

                    reason = recovery.failure_reason(output)
                    if attempts >= recovery.max_attempts():
                        break
                    if not reviewer.budget.allow_retry():
                        logger.warning(
                            "Agent Loop: '%s' retry budget exhausted (%s)",
                            task.get("id"), reason,
                        )
                        break

                    transient = recovery.classify_failure(reason) == recovery.TRANSIENT
                    reviewer.budget.consume_retry()
                    delay = recovery.backoff_seconds(attempts) if transient else 0.0
                    retry_events.append({"event": "agent_retry", "data": {
                        "run_id": reviewer.run_id,
                        "agent": agent,
                        "task_id": task.get("id", ""),
                        "label": agent_labels.get(agent_map.get(agent, ""), agent),
                        "attempt": attempts + 1,
                        "max_attempts": recovery.max_attempts(),
                        "kind": recovery.TRANSIENT if transient else recovery.SEMANTIC,
                        "reason": reason,
                        "backoff_seconds": round(delay, 1),
                    }})
                    reviewer.record(
                        "agent.retry", stage="wave", target=task.get("id", ""),
                        decision="allow",
                        risk="medium" if not transient else "low",
                        severity="warning",
                        message=f"{agent} 第 {attempts + 1} 次尝试：{reason}",
                        details={"kind": "transient" if transient else "semantic",
                                 "delay": delay},
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    current = recovery.retry_task(task, attempts, reason, transient=transient)

                return task, output, attempts, retry_events, time.monotonic() - started

            wave_future = asyncio.gather(*[
                _run_one(task, fn, state) for task, fn in approved_wave
            ])

            while True:
                done, _ = await asyncio.wait([wave_future], timeout=30.0)
                if done:
                    break
                yield {"event": "heartbeat", "data": {}}

            results = await wave_future

            # Merge and emit agent_done
            for task, output, attempts, retry_events, task_duration in results:
                agent = task.get("agent", "")

                for event in retry_events:
                    yield event

                # ── Safety checkpoint 3: output review + redaction ──
                output_decision = None
                if isinstance(output, dict):
                    output_decision = reviewer.review_output(task, output)
                    reviewer.record_decision(output_decision, target=task.get("id", ""))
                    if output_decision.findings or output_decision.decision != "allow":
                        yield safety_review_event(output_decision, "output")
                    output, _ = reviewer.scrub_output(output)

                merge_worker_results(state, [output])
                completed.add(task.get("id", ""))

                # Accumulate for the final event
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

                node_name = agent_map.get(agent, f"{agent}_worker")
                summary = _summarize_output(agent, output)

                # Record the task in the run trace (usage, timings, failures).
                try:
                    from backend.observability import TaskRecord, current_trace

                    trace = current_trace()
                    if trace is not None:
                        trace.add_task(TaskRecord(
                            agent=agent,
                            task_id=task.get("id", ""),
                            attempts=attempts,
                            duration=task_duration,
                            status="failed" if recovery.is_unusable(output) else "done",
                            result_key=summary.get("result_key", ""),
                            count=summary.get("count", 0),
                        ))
                except Exception as trace_err:
                    logger.debug("Task trace skipped: %s", trace_err)

                done_data = {
                    "agent": task.get("id") or node_name,
                    "agent_type": agent,
                    "label": agent_labels.get(node_name, agent),
                    "task_id": task.get("id", ""),
                    "slide_index": task.get("slide_index"),
                    "status": "done",
                    "attempts": attempts,
                    "duration": round(task_duration, 2),
                    **summary,
                }
                if output_decision is not None and output_decision.findings:
                    done_data["review"] = {
                        "risk": output_decision.risk,
                        "summary": output_decision.summary,
                    }
                yield {"event": "agent_done", "data": done_data}

        # ── Safety checkpoint 4: goal completion confirmation ──
        tasks = _normalize_tasks(state.get("tasks", []))
        yield {"event": "phase", "data": {"phase": "verify", "message": "确认目标完成情况..."}}
        verdict = await reviewer.verify(user_request, tasks, state)
        state["_verification"] = verdict
        yield {"event": "verification", "data": verdict}

        # A remediation round must never restart an already exhausted budget.
        if budget_exhausted or not reviewer.budget.allow_replan():
            break

        remediation, strategy, reason = await _propose_recovery(
            user_request, tasks, state, verdict, reviewer
        )
        if not remediation:
            break

        reviewer.budget.consume_replan()
        reviewer.record(
            "verification.remediation", stage="verify", decision="allow", risk="medium",
            severity="warning",
            message=f"追加 {len(remediation)} 个补救步骤（{strategy}，第 {reviewer.budget.replans} 轮）",
            details={"strategy": strategy, "reason": reason,
                     "tasks": [t.get("agent") for t in remediation]},
        )
        state["tasks"] = _normalize_tasks(tasks + remediation)
        yield {"event": "remediation", "data": {
            "run_id": reviewer.run_id,
            "round": reviewer.budget.replans,
            "strategy": strategy,
            "reason": reason,
            "tasks": [
                {"agent": t.get("agent", ""), "prompt": (t.get("prompt") or "")[:120]}
                for t in remediation
            ],
        }}
        continue

    state["_safety"] = reviewer.summary()
    state["_stopped_reason"] = stopped_reason

    yield {"event": "phase", "data": {"phase": "synthesize", "message": "synthesizing..."}}
    yield state_event()


async def run_agent_loop_stream(
    initial_state: dict,
    agent_labels: dict[str, str],
    agent_map: dict[str, str],
) -> AsyncGenerator[dict, None]:
    """Streaming entry point — forwards every core event to the SSE layer."""
    async for event in _run_agent_loop_events(
        initial_state, agent_labels, agent_map, streaming=True
    ):
        yield event


async def run_agent_loop(initial_state: dict) -> dict:
    """Non-streaming entry point used by ``/api/chat/sync``.

    Drains the shared core and keeps only the final state, then runs the
    synthesizer unless the caller asked to skip it.
    """
    state = dict(initial_state)

    async for event in _run_agent_loop_events(state, {}, {}, streaming=False):
        if event.get("event") == "_agent_loop_done":
            state = event["data"]["state"]

    if not state.get("_skip_synthesizer") and not state.get("final_response"):
        logger.info("Agent Loop: running synthesizer...")
        synth_result = await synthesizer_node(state)
        merge_worker_results(state, [synth_result])

    return state
