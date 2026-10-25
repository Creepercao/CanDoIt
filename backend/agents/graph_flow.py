"""LangGraph orchestration backbone — hybrid mode.

Shape of the graph::

    START → memory → plan → review_plan → gate ⇄ execute
                                            gate → verify
                                            verify → remediate → gate
                                            verify → remediate → END

Wave *scheduling* is still the original "rescan every round" loop, but it now
lives inside ``gate_node`` (which decides) and ``execute_node`` (which runs one
wave).  Concurrency inside a wave stays ``asyncio.gather`` — that is the
"hybrid" part: graph-level control flow, hand-written fan-out inside it.

Why approval is its own node
----------------------------
``interrupt()`` re-runs the interrupted node from its first line when resumed.
If approval sat inside the wave executor, resuming would replay a partially
finished wave.  ``gate_node`` therefore handles **one** pending approval per
visit and returns, so every resume lands on a stable, cheap position.  Approved
decisions are persisted in state (``_decisions``) and never asked twice.

Node signatures take ``dict``, not ``AgentState``
--------------------------------------------------
LangGraph inspects a node's parameter annotation and only passes the channels
that annotation declares.  Annotating nodes with the static ``AgentState``
silently dropped dynamically declared channels (custom skills' ``*_results``),
so the nodes are annotated ``dict`` and the graph schema is built at compile
time by :func:`_state_schema`.

Synthesis is deliberately *not* a node: the stream runner keeps doing it so the
final answer is still streamed token by token (see ``agents.runner``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.agents import orchestrator as orch
from backend.mq import get_event_bus
from backend.safety.budget import RunBudget
from backend.safety.reviewer import SafetyReviewer

logger = logging.getLogger("agents.graph")


class AgentState(TypedDict, total=False):
    """Static part of the run state.

    ``total=False`` because a freshly planned run has none of the result keys.
    Custom skills add their own ``*_results`` channels at compile time; see
    :func:`_state_schema`.
    """

    # ── original orchestrator state ──
    messages: list
    user_request: str
    tasks: list
    plan_steps: list
    research_results: list
    analyst_results: list
    chart_results: list
    image_results: list
    video_results: list
    code_results: list
    skill_outputs: dict
    final_response: str
    chat_model_id: str
    image_model_id: str
    video_model_id: str
    router_model_id: str
    session_id: str
    run_id: str
    safety_blocks: list
    dropped_tasks: list
    _skip_synthesizer: bool
    _refused: bool
    _terminal_emitted: bool
    _stopped_reason: str
    _verification: dict
    _safety: dict
    _memories: str
    _recalled_memories: list

    # ── graph-internal bookkeeping ──
    _completed: list
    _decisions: dict
    _budget: dict
    _gate_stage: str
    _needs_remediation: bool
    _accumulated: dict
    _collected_html: list
    _streaming: bool
    _agent_labels: dict


# ── run-scoped reviewer registry ───────────────────────────────────────

_REVIEWERS: dict[str, SafetyReviewer] = {}


def _reviewer_for(state: dict) -> SafetyReviewer:
    """Return this run's reviewer, rebuilt from the checkpointed budget.

    A resumed run may be served by a different process, so the reviewer cannot
    live only in a module dict — the budget counters ride along in state.
    """
    run_id = state.get("run_id") or ""
    reviewer = _REVIEWERS.get(run_id)
    if reviewer is not None:
        return reviewer

    budget = RunBudget.from_env()
    snapshot = state.get("_budget") or {}
    for key in ("steps", "waves", "replans", "llm_reviews", "retries"):
        if key in snapshot:
            try:
                setattr(budget, key, int(snapshot[key]))
            except (TypeError, ValueError):
                pass

    reviewer = SafetyReviewer(
        run_id or "unknown-run",
        session_id=state.get("session_id", "") or "",
        streaming=bool(state.get("_streaming", True)),
        budget=budget,
        model_id=state.get("chat_model_id", "") or "",
    )
    _REVIEWERS[run_id] = reviewer
    return reviewer


def release_reviewer(run_id: str) -> None:
    """Drop the cached reviewer once a run is finished."""
    _REVIEWERS.pop(run_id, None)


def _budget_field(state: dict) -> dict:
    return {"_budget": _reviewer_for(state).budget.snapshot()}


# ── event publishing ───────────────────────────────────────────────────


async def _emit(state: dict, event: str, data: dict) -> None:
    """Publish one SSE-shaped event to the run's bus."""
    run_id = state.get("run_id") or ""
    if not run_id:
        return
    try:
        await get_event_bus().publish(run_id, event, data)
    except Exception as exc:  # never let telemetry break a run
        logger.debug("event publish failed (%s): %s", event, exc)


def _label(agent: str, state: dict) -> str:
    labels = state.get("_agent_labels") or {}
    return labels.get(agent, agent)


# ── execution chain wiring ─────────────────────────────────────────────
#
# The run's root span is opened by the driver (it owns the run lifetime) and
# its id is published here, because graph nodes are separate functions that
# cannot receive it as an argument.

_RUN_SPANS: dict[str, str] = {}


def set_run_span(run_id: str, span_id: str) -> None:
    _RUN_SPANS[run_id] = span_id


def clear_run_span(run_id: str) -> None:
    _RUN_SPANS.pop(run_id, None)


def _root(state: dict) -> str:
    return _RUN_SPANS.get(state.get("run_id", ""), "")


def _chain():
    """Active run chain, or a no-op chain when tracing is off."""
    try:
        from backend.observability import current_trace
        from backend.observability.chain import RunChain

        trace = current_trace()
        return trace.chain if trace is not None else RunChain.disabled()
    except Exception:
        from backend.observability.chain import RunChain

        return RunChain.disabled()


# ── helpers borrowed from the original generator ───────────────────────


def _tasks(state: dict) -> list[dict]:
    return orch._normalize_tasks(state.get("tasks") or [])


def _completed_ids(state: dict) -> set[str]:
    return {str(x) for x in (state.get("_completed") or [])}


def _refusal(state: dict, decision, note: str = "") -> dict:
    text = orch._refusal_text(decision, note if note != "被安全策略拦截" else "")
    reviewer = _reviewer_for(state)
    return {
        "final_response": text,
        "_refused": True,
        "_stopped_reason": f"计划审查未通过（{note or decision.summary}）",
        "_safety": reviewer.summary(),
        **_budget_field(state),
    }


def _answer_approved(answer: Any) -> bool:
    """Interpret a resume payload for an approval interrupt."""
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, dict):
        for key in ("approved", "allow", "ok"):
            if key in answer:
                return bool(answer[key])
        decision = str(answer.get("decision", "")).lower()
        if decision:
            return decision in ("approve", "approved", "allow", "allowed")
    return False


# ── nodes ──────────────────────────────────────────────────────────────


async def memory_node(state: dict) -> dict:
    """Recall cross-session memories so planning respects preferences."""
    await _emit(state, "phase", {"phase": "supervisor", "message": "analyzing request..."})

    block = ""
    recalled: list = []
    chain = _chain()
    from backend.observability import KIND_MEMORY

    with chain.span(KIND_MEMORY, "召回长期记忆", parent_id=_root(state)) as mem_span:
        try:
            from backend.memory import get_memory_store, memory_enabled

            if memory_enabled():
                block, recalled = await get_memory_store().recall_for_prompt(
                    state.get("user_request", "")
                )
                block = block or ""
                mem_span.set(recalled=len(recalled))
                if recalled:
                    await _emit(state, "memory", {
                        "run_id": state.get("run_id", ""),
                        "count": len(recalled),
                        "items": [
                            {"kind": item["kind"], "text": item["text"],
                             "score": item.get("score")}
                            for item in recalled
                        ],
                    })
            else:
                mem_span.set(status="skipped", reason="AGENT_MEMORY=0")
        except Exception as exc:
            logger.debug("Memory recall skipped: %s", exc)
            mem_span.set(status="skipped", error=exc)

    return {"_memories": block, "_recalled_memories": recalled}


async def plan_node(state: dict) -> dict:
    """Supervisor planning + plan validation + agent screening."""
    reviewer = _reviewer_for(state)
    run_id = state.get("run_id", "")

    chain = _chain()
    from backend.observability import KIND_PLAN

    with chain.span(KIND_PLAN, "supervisor 规划", parent_id=_root(state)) as plan_span:
        sup_result = await orch.supervisor_node(dict(state))
        plan_span.set(
            steps=len(sup_result.get("tasks") or []),
            direct=bool(sup_result.get("final_response")),
        )
    merged = orch.merge_worker_results(dict(state), [sup_result])

    if merged.get("final_response"):
        text = merged["final_response"]
        await _emit(state, "phase", {"phase": "done", "message": "direct reply"})
        await _emit(state, "final", {
            "response": text,
            "run_id": run_id,
            "safety": reviewer.summary(),
        })
        await _emit(state, "done", {})
        # The graph published its own terminal frames. Callers that would also
        # emit them (the Celery worker) must skip, or the client sees the
        # answer twice.
        return {
            "final_response": text,
            "_terminal_emitted": True,
            **_budget_field(state),
        }

    tasks = orch._normalize_tasks(merged.get("tasks") or [])
    tasks = orch._normalize_tasks(
        orch.validate_and_complete_plan(tasks, state.get("user_request", ""))
    )
    tasks, dropped = reviewer.screen_tasks(tasks)

    plan_steps = merged.get("plan_steps") or []
    labels = state.get("_agent_labels") or {}
    await _emit(state, "plan", {
        "run_id": run_id,
        "tasks": [
            {"agent": t.get("agent", ""),
             "label": labels.get(t.get("agent", ""), t.get("agent", "")),
             "prompt": (t.get("prompt") or "")[:120]}
            for t in tasks
        ],
        "count": len(tasks),
        "dropped": dropped,
        "steps": [
            {"step": s.get("step", i + 1),
             "agent": s.get("agent", ""),
             "label": labels.get(s.get("agent", ""), s.get("agent", "")),
             "prompt": (s.get("prompt") or "")[:120],
             "depends_on": s.get("depends_on", []),
             "reason": s.get("reason", "")}
            for i, s in enumerate(plan_steps)
        ] if plan_steps else [],
    })

    if not tasks:
        return {
            "tasks": [],
            "dropped_tasks": dropped,
            "final_response": (
                "计划中的步骤都引用了当前未注册的代理，无法执行。请检查技能是否已启用。"
            ),
            "_stopped_reason": "计划中没有可执行的步骤（代理未注册）",
            **_budget_field(state),
        }

    return {
        "tasks": tasks,
        "plan_steps": plan_steps,
        "dropped_tasks": dropped,
        **_budget_field(state),
    }


async def review_plan_node(state: dict) -> dict:
    """Safety checkpoint 1 — plan review (with optional human approval)."""
    reviewer = _reviewer_for(state)
    run_id = state.get("run_id", "")
    tasks = _tasks(state)
    if not tasks:
        return {}

    await _emit(state, "phase", {"phase": "review", "message": "执行审查中..."})

    chain = _chain()
    from backend.observability import KIND_REVIEW

    with chain.span(KIND_REVIEW, "计划审查", parent_id=_root(state)) as review_span:
        decision = reviewer.review_plan(state.get("user_request", ""), tasks)
        decision = await reviewer.enrich_plan_review(
            state.get("user_request", ""), tasks, decision
        )
        review_span.set(decision=decision.decision, risk=decision.risk,
                        findings=len(decision.findings))
    reviewer.record_decision(decision, target="plan")
    await _emit(state, "safety_review", {
        "run_id": run_id, "gate": "plan", **decision.to_dict(),
    })

    if decision.blocked:
        return _refusal(state, decision)

    if decision.requires_approval:
        approval_id = uuid.uuid4().hex
        answer = interrupt({
            "run_id": run_id,
            "approval_id": approval_id,
            "gate": "plan",
            "risk": decision.risk,
            "summary": decision.summary,
            "reasons": decision.reasons[:5],
            "tasks": [t.get("agent", "") for t in tasks],
        })
        approved = _answer_approved(answer)
        await _emit(state, "approval_resolved", {
            "run_id": run_id,
            "approval_id": approval_id,
            "status": "approved" if approved else "rejected",
            "decided_by": (answer or {}).get("decided_by", "human")
            if isinstance(answer, dict) else "human",
        })
        if not approved:
            return _refusal(state, decision, "审批未通过（human）")

    return {**_budget_field(state)}


async def gate_node(state: dict) -> dict:
    """Decide the next wave and resolve exactly one pending approval."""
    reviewer = _reviewer_for(state)
    run_id = state.get("run_id", "")

    tasks = _tasks(state)
    completed = _completed_ids(state)
    all_ids = {t.get("id", "") for t in tasks}

    if completed >= all_ids:
        return {"_gate_stage": "done", **_budget_field(state)}

    if not reviewer.budget.allow_wave():
        reason = orch._budget_note(reviewer)
        await _emit(state, "budget_exceeded", {
            "run_id": run_id,
            "reason": reason,
            "budget": reviewer.budget.snapshot(),
        })
        return {
            "_gate_stage": "done",
            "_stopped_reason": reason,
            **_budget_field(state),
        }

    wave = [
        task for task in tasks
        if task.get("id", "") not in completed
        and all(d in completed for d in orch._task_deps(task, tasks))
    ]

    if not wave:
        pending = all_ids - completed
        logger.warning(
            "Agent Loop: deadlock — completed=%s, pending=%s", completed, pending
        )
        return {
            "_gate_stage": "done",
            "_stopped_reason": "检测到任务依赖死锁，提前结束执行",
            **_budget_field(state),
        }

    decisions = dict(state.get("_decisions") or {})

    # Resolve approvals one at a time: a single stable interrupt position.
    for task in wave:
        task_id = task.get("id", "")
        if task_id in decisions:
            continue

        decision = reviewer.review_action(task)
        reviewer.record_decision(decision, target=task_id)

        if decision.requires_approval:
            await _emit(state, "safety_review", {
                "run_id": run_id, "gate": "action", **decision.to_dict(),
            })
            approval_id = uuid.uuid4().hex
            answer = interrupt({
                "run_id": run_id,
                "approval_id": approval_id,
                "gate": "action",
                "risk": decision.risk,
                "summary": decision.summary,
                "reasons": decision.reasons[:5],
                "task": task.get("prompt", "")[:180],
                "agent": task.get("agent", ""),
                "task_id": task_id,
            })
            approved = _answer_approved(answer)
            decisions[task_id] = bool(approved)
            await _emit(state, "approval_resolved", {
                "run_id": run_id,
                "approval_id": approval_id,
                "status": "approved" if approved else "rejected",
                "decided_by": (answer or {}).get("decided_by", "human")
                if isinstance(answer, dict) else "human",
            })
            return {"_decisions": decisions, "_gate_stage": "pending",
                    **_budget_field(state)}

        if decision.blocked:
            decisions[task_id] = False
            await _emit(state, "safety_review", {
                "run_id": run_id, "gate": "action", **decision.to_dict(),
            })
            blocks = list(state.get("safety_blocks") or [])
            blocks.append(orch._block_record(task, decision, "action", "被安全策略拦截"))
            # A blocked task counts as "resolved" so dependants are not stuck.
            completed.add(task_id)
            return {
                "_decisions": decisions,
                "_gate_stage": "pending",
                "_completed": sorted(completed),
                "safety_blocks": blocks,
                **_budget_field(state),
            }

        decisions[task_id] = True

    return {"_decisions": decisions, "_gate_stage": "run", **_budget_field(state)}


async def _run_one_task(state: dict, task: dict, worker_fn: Callable) -> dict:
    """Execute a single task with the retry policy from the original loop."""
    from backend.agents import recovery
    from backend.observability import set_current_agent

    agent = task.get("agent", "")
    set_current_agent(agent)

    reviewer = _reviewer_for(state)
    chain = _chain()
    from backend.observability import KIND_ATTEMPT, KIND_TASK

    attempts = 0
    output: dict = {}
    current = task
    started = time.monotonic()

    task_span = chain.start(
        KIND_TASK, agent, agent=agent, parent_id=_root(state),
        task_id=task.get("id", ""),
        prompt=(task.get("prompt") or "")[:100],
    )

    while True:
        attempts += 1
        attempt_span = chain.start(
            KIND_ATTEMPT, f"第 {attempts} 次尝试", agent=agent,
            parent_id=task_span.span_id,
        )
        try:
            timeout = orch._task_timeout_seconds(agent)
            output = await asyncio.wait_for(
                worker_fn(orch._state_for_task(dict(state), current)), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error("Agent Loop: '%s' timed out", task.get("id"))
            output = {f"{agent}_results": [{"error": "task timed out"}]}
        except Exception as exc:
            logger.error("Agent Loop: '%s' error: %s", task.get("id"), exc)
            output = {f"{agent}_results": [{"error": str(exc)}]}

        if not isinstance(output, dict):
            output = {f"{agent}_results": [{"error": "worker returned a non-dict result"}]}

        usable = not recovery.is_unusable(output)
        if usable:
            chain.finish(attempt_span, ok=True)
            break

        reason = recovery.failure_reason(output)
        chain.finish(attempt_span, status="failed", error=reason)

        if attempts >= recovery.max_attempts():
            break
        if not reviewer.budget.allow_retry():
            break

        transient = recovery.classify_failure(reason) == recovery.TRANSIENT
        reviewer.budget.consume_retry()
        task_span.set(status="retried", attempts=attempts)
        delay = recovery.backoff_seconds(attempts) if transient else 0.0
        await _emit(state, "agent_retry", {
            "run_id": state.get("run_id", ""),
            "agent": agent,
            "task_id": task.get("id", ""),
            "label": _label(agent, state),
            "attempt": attempts + 1,
            "max_attempts": recovery.max_attempts(),
            "kind": recovery.TRANSIENT if transient else recovery.SEMANTIC,
            "reason": reason,
            "backoff_seconds": round(delay, 1),
        })
        reviewer.record(
            "agent.retry", stage="wave", target=task.get("id", ""),
            decision="allow", risk="medium" if not transient else "low",
            severity="warning",
            message=f"{agent} 第 {attempts + 1} 次尝试：{reason}",
            details={"kind": "transient" if transient else "semantic", "delay": delay},
        )
        if delay:
            await asyncio.sleep(delay)
        current = recovery.retry_task(task, attempts, reason, transient=transient)

    if recovery.is_unusable(output):
        chain.finish(task_span, status="failed",
                     error=recovery.failure_reason(output), attempts=attempts)
    elif attempts > 1:
        # Succeeded, but only after a retry — say so in the chain.
        chain.finish(task_span, status="retried", attempts=attempts)
    else:
        chain.finish(task_span, attempts=attempts)

    return {
        "task": task,
        "output": output,
        "attempts": attempts,
        "duration": time.monotonic() - started,
        "unusable": recovery.is_unusable(output),
    }


async def execute_node(state: dict) -> dict:
    """Run the current wave in parallel and merge the results."""
    reviewer = _reviewer_for(state)
    run_id = state.get("run_id", "")
    tasks = _tasks(state)
    completed = _completed_ids(state)
    decisions = state.get("_decisions") or {}

    workers = orch._get_worker_registry()
    wave = [
        task for task in tasks
        if task.get("id", "") not in completed
        and all(d in completed for d in orch._task_deps(task, tasks))
        and decisions.get(task.get("id", "")) is not False
        and workers.get(task.get("agent", ""))
    ]

    # Tasks marked rejected earlier must not stall the wave loop.
    blocked_now = [
        task for task in tasks
        if task.get("id", "") not in completed
        and all(d in completed for d in orch._task_deps(task, tasks))
        and decisions.get(task.get("id", "")) is False
    ]

    blocks = list(state.get("safety_blocks") or [])

    if blocked_now:
        for task in blocked_now:
            blocks.append({
                "task_id": task.get("id", ""),
                "agent": task.get("agent", ""),
                "stage": "action",
                "risk": "high",
                "summary": "审批未通过",
                "reasons": ["human rejected"],
                "note": "审批未通过（human）",
            })
            completed.add(task.get("id", ""))
            await _emit(state, "agent_done", {
                "agent": task.get("id") or task.get("agent", ""),
                "agent_type": task.get("agent", ""),
                "label": _label(task.get("agent", ""), state),
                "task_id": task.get("id", ""),
                "status": "blocked",
                "result_key": "",
                "count": 0,
                "summary": "已拦截：审批未通过",
            })
        if not wave:
            return {
                "_completed": sorted(completed),
                "safety_blocks": blocks,
                **_budget_field(state),
            }

    if not wave:
        return {"_completed": sorted(completed), **_budget_field(state)}

    if not reviewer.budget.allow_steps(len(wave)):
        reason = orch._budget_note(reviewer)
        await _emit(state, "budget_exceeded", {
            "run_id": run_id, "reason": reason,
            "budget": reviewer.budget.snapshot(),
        })
        return {"_gate_stage": "done", "_stopped_reason": reason,
                **_budget_field(state)}

    reviewer.budget.consume_wave()
    reviewer.budget.consume_steps(len(wave))

    for task in wave:
        await _emit(state, "agent_start", {
            "agent": task.get("id") or task.get("agent", ""),
            "agent_type": task.get("agent", ""),
            "label": _label(task.get("agent", ""), state),
            "task": task.get("prompt", "")[:180],
            "task_count": 1,
            "task_id": task.get("id", ""),
            "slide_index": task.get("slide_index"),
        })

    gathered = asyncio.gather(*[
        _run_one_task(state, task, workers[task["agent"]]) for task in wave
    ])
    while True:
        done, _ = await asyncio.wait([gathered], timeout=30.0)
        if done:
            break
        await _emit(state, "heartbeat", {})

    results = list(await gathered)

    merged_state = dict(state)
    accumulated = dict(state.get("_accumulated") or {})
    collected_html = list(state.get("_collected_html") or [])

    for item in results:
        task = item["task"]
        output = item["output"]
        agent = task.get("agent", "")

        # Safety checkpoint 3 — output review + redaction.
        decision = None
        if isinstance(output, dict):
            decision = reviewer.review_output(task, output)
            reviewer.record_decision(decision, target=task.get("id", ""))
            if decision.findings or decision.decision != "allow":
                await _emit(state, "safety_review", {
                    "run_id": run_id, "gate": "output", **decision.to_dict(),
                })
            output, _redacted = reviewer.scrub_output(output)

        orch.merge_worker_results(merged_state, [output])
        completed.add(task.get("id", ""))

        for key in orch.RESULT_KEYS:
            if key in output and output[key]:
                value = output[key]
                if key == "skill_outputs" and isinstance(value, dict):
                    target = accumulated.setdefault(key, {})
                    for sk_name, items in value.items():
                        target.setdefault(sk_name, []).extend(items or [])
                    from backend.api import _HTML_DETECT_RE, save_skill_html
                    for sk_name, items in value.items():
                        for entry in (items or []):
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("html_url"):
                                collected_html.append({
                                    "skill_name": sk_name,
                                    "html_url": entry["html_url"],
                                    "title": entry.get("html_title", ""),
                                    "task": entry.get("task", ""),
                                })
                                continue
                            text = entry.get("result", "")
                            if isinstance(text, str) and _HTML_DETECT_RE.search(text[:500]):
                                saved = save_skill_html(
                                    sk_name, text, title=entry.get("task", "")
                                )
                                entry["html_url"] = saved["html_url"]
                                entry["html_title"] = saved["title"]
                                collected_html.append({
                                    "skill_name": sk_name,
                                    "html_url": entry["html_url"],
                                    "title": entry.get("html_title", ""),
                                    "task": entry.get("task", ""),
                                })
                else:
                    accumulated.setdefault(key, []).extend(
                        value if isinstance(value, list) else [value]
                    )

        summary = orch._summarize_output(agent, output)

        try:
            from backend.observability import TaskRecord, current_trace

            trace = current_trace()
            if trace is not None:
                trace.add_task(TaskRecord(
                    agent=agent,
                    task_id=task.get("id", ""),
                    attempts=item["attempts"],
                    duration=item["duration"],
                    status="failed" if item["unusable"] else "done",
                    result_key=summary.get("result_key", ""),
                    count=summary.get("count", 0),
                ))
        except Exception as exc:
            logger.debug("Task trace skipped: %s", exc)

        done_data = {
            "agent": task.get("id") or task.get("agent", ""),
            "agent_type": agent,
            "label": _label(agent, state),
            "task_id": task.get("id", ""),
            "slide_index": task.get("slide_index"),
            "status": "done",
            "attempts": item["attempts"],
            "duration": round(item["duration"], 2),
            **summary,
        }
        if decision is not None and decision.findings:
            done_data["review"] = {"risk": decision.risk, "summary": decision.summary}
        await _emit(state, "agent_done", done_data)

    result: dict[str, Any] = {
        "tasks": merged_state.get("tasks") or tasks,
        "research_results": merged_state.get("research_results", []),
        "analyst_results": merged_state.get("analyst_results", []),
        "chart_results": merged_state.get("chart_results", []),
        "image_results": merged_state.get("image_results", []),
        "video_results": merged_state.get("video_results", []),
        "code_results": merged_state.get("code_results", []),
        "skill_outputs": merged_state.get("skill_outputs") or {},
        "messages": merged_state.get("messages", []),
        "_completed": sorted(completed),
        "_accumulated": accumulated,
        "_collected_html": collected_html,
        "safety_blocks": merged_state.get("safety_blocks") or blocks,
        **_budget_field(state),
    }
    # Custom skills write their own ``*_results`` channels; carry them through.
    for key, value in merged_state.items():
        if key.endswith("_results") and key not in result:
            result[key] = value
    return result


async def verify_node(state: dict) -> dict:
    """Safety checkpoint 4 — goal completion confirmation."""
    reviewer = _reviewer_for(state)
    await _emit(state, "phase", {"phase": "verify", "message": "确认目标完成情况..."})

    tasks = _tasks(state)
    chain = _chain()
    from backend.observability import KIND_VERIFY

    with chain.span(KIND_VERIFY, "目标完成确认", parent_id=_root(state)) as verify_span:
        verdict = await reviewer.verify(
            state.get("user_request", ""), tasks, dict(state)
        )
        verify_span.set(
            status={"complete": "ok", "partial": "retried",
                    "incomplete": "retried", "failed": "failed"}.get(
                        verdict.get("status", ""), "ok"),
            verdict=verdict.get("status", ""),
            score=verdict.get("score"),
            unmet=len(verdict.get("unmet") or []),
        )
    reviewer.verdict = verdict
    await _emit(state, "verification", verdict)
    return {"_verification": verdict, **_budget_field(state)}


async def remediate_node(state: dict) -> dict:
    """Propose recovery steps for an incomplete run."""
    reviewer = _reviewer_for(state)
    run_id = state.get("run_id", "")
    tasks = _tasks(state)
    verdict = state.get("_verification") or {}

    if not reviewer.budget.allow_replan():
        return {"_needs_remediation": False, **_budget_field(state)}

    remediation, strategy, reason = await orch._propose_recovery(
        state.get("user_request", ""), tasks, dict(state), verdict, reviewer
    )
    if not remediation:
        return {"_needs_remediation": False, **_budget_field(state)}

    reviewer.budget.consume_replan()
    chain = _chain()
    from backend.observability import KIND_REMEDIATE

    chain.add(
        KIND_REMEDIATE, "补救回合", parent_id=_root(state),
        status="retried", strategy=strategy, reason=reason,
        round=reviewer.budget.replans,
        agents=",".join(t.get("agent", "") for t in remediation),
    )
    reviewer.record(
        "verification.remediation", stage="verify", decision="allow", risk="medium",
        severity="warning",
        message=f"追加 {len(remediation)} 个补救步骤（{strategy}，第 {reviewer.budget.replans} 轮）",
        details={"strategy": strategy, "reason": reason,
                 "tasks": [t.get("agent") for t in remediation]},
    )
    await _emit(state, "remediation", {
        "run_id": run_id,
        "round": reviewer.budget.replans,
        "strategy": strategy,
        "reason": reason,
        "tasks": [
            {"agent": t.get("agent", ""),
             "prompt": (t.get("prompt") or "")[:120]}
            for t in remediation
        ],
    })

    return {
        "tasks": orch._normalize_tasks(tasks + remediation),
        "_needs_remediation": True,
        **_budget_field(state),
    }


# ── routing ────────────────────────────────────────────────────────────


def route_after_plan(state: dict) -> str:
    if state.get("final_response"):
        return "end"
    if state.get("_refused"):
        return "end"
    if not (state.get("tasks") or []):
        return "end"
    return "review"


def route_after_review(state: dict) -> str:
    if state.get("_refused") or state.get("final_response"):
        return "end"
    return "gate"


def route_gate(state: dict) -> str:
    stage = state.get("_gate_stage") or "run"
    if stage == "pending":
        return "gate"
    if stage == "done":
        return "verify"
    return "execute"


def route_remediate(state: dict) -> str:
    if state.get("_needs_remediation"):
        return "gate"
    return "end"


# ── graph assembly ─────────────────────────────────────────────────────


def _state_schema():
    """Build the graph schema, including dynamically declared result channels.

    A custom skill owns its ``result_key`` (e.g. ``flaky_results``).  LangGraph
    drops any state key that is not a declared channel, so those keys have to be
    discovered from the registry at build time rather than hard-coded here.
    """
    annotations = dict(AgentState.__annotations__)
    try:
        from backend.skills.registry import skill_registry

        for skill in skill_registry.get_enabled().values():
            key = getattr(skill, "result_key", "") or ""
            if key and key not in annotations:
                # Match the string-annotation style of ``AgentState`` (this
                # module uses ``from __future__ import annotations``).
                annotations[key] = "list"
    except Exception as exc:  # registry unavailable — built-ins still work
        logger.debug("Dynamic state channels skipped: %s", exc)
    return TypedDict("GraphAgentState", annotations, total=False)  # type: ignore[misc]


def build_graph():
    """Build and return the uncompiled graph."""
    graph = StateGraph(_state_schema())

    graph.add_node("memory", memory_node)
    graph.add_node("plan", plan_node)
    graph.add_node("review_plan", review_plan_node)
    graph.add_node("gate", gate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("verify", verify_node)
    graph.add_node("remediate", remediate_node)

    graph.add_edge(START, "memory")
    graph.add_edge("memory", "plan")

    graph.add_conditional_edges("plan", route_after_plan, {
        "review": "review_plan",
        "end": END,
    })
    graph.add_conditional_edges("review_plan", route_after_review, {
        "gate": "gate",
        "end": END,
    })
    graph.add_conditional_edges("gate", route_gate, {
        "gate": "gate",
        "execute": "execute",
        "verify": "verify",
    })
    graph.add_edge("execute", "gate")
    # verify always hands over to remediate; that node decides whether a
    # recovery round is warranted (it owns the budget and the gap analysis).
    graph.add_edge("verify", "remediate")
    graph.add_conditional_edges("remediate", route_remediate, {
        "gate": "gate",
        "end": END,
    })

    return graph


def recursion_limit() -> int:
    """Graph recursion guard; generous because gate/execute ping-pong."""
    raw = os.environ.get("AGENT_GRAPH_RECURSION_LIMIT", "200")
    try:
        return max(25, int(raw))
    except (TypeError, ValueError):
        return 200


def compile_graph(cp=None):
    """Compile the graph with an optional checkpointer."""
    return build_graph().compile(checkpointer=cp)
