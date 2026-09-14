"""Failure classification, task retry and incremental re-planning.

Before this module a single transient provider error — a 429, a dropped
connection, one empty completion — permanently killed its step: the wave moved
on, the artifact was missing, and the synthesizer had to apologise.  Two
mechanisms now recover from that:

* **Task retry** — an unusable result is re-run with the failure fed back into
  the prompt (``retry_task``), so the worker gets a chance to correct itself.
  Transient failures are retried unchanged after an exponential backoff;
  semantic failures (empty/garbage output) are retried with explicit feedback.
* **Incremental re-planning** — when steps ultimately fail or the goal
  confirmation finds gaps, the supervisor is asked for a *revised* set of
  remaining steps (``replan``) instead of blindly replaying the same tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional

from langchain_core.messages import HumanMessage

from backend.safety import policy

logger = logging.getLogger("agents.recovery")

TRANSIENT = "transient"
SEMANTIC = "semantic"

# Provider/transport hiccups that a plain retry can fix.
_TRANSIENT_RE = re.compile(
    r"(?i)\b(?:429|408|409|425|5\d\d)\b"
    r"|rate[\s_-]?limit|too many requests|quota"
    r"|time[\s_-]?out|timed out|deadline exceeded"
    r"|connection (?:error|reset|aborted|refused)|econnreset|econnrefused|epipe"
    r"|temporarily unavailable|service unavailable|overload|bad gateway"
    r"|read timed out|incomplete read|remote end closed"
)

_MAX_STEPS = 4


# ── configuration ──────────────────────────────────────────────────────


def _env_int(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(floor, int(float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, floor: float = 0.0) -> float:
    try:
        return max(floor, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def max_attempts() -> int:
    """Attempts allowed for one task (1 = no retry)."""
    return _env_int("AGENT_TASK_MAX_ATTEMPTS", 3)


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff for the *attempt*-th failure (0 disables sleeping)."""
    base = _env_float("AGENT_RETRY_BASE_SECONDS", 2.0)
    cap = _env_float("AGENT_RETRY_MAX_SECONDS", 20.0)
    if base <= 0:
        return 0.0
    return min(cap, base * (2 ** max(0, attempt - 1)))


def llm_replan_enabled() -> bool:
    raw = os.environ.get("AGENT_LLM_REPLAN")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── failure inspection ─────────────────────────────────────────────────


def classify_failure(text: str) -> str:
    """``transient`` when a retry may succeed, otherwise ``semantic``."""
    if text and _TRANSIENT_RE.search(text):
        return TRANSIENT
    return SEMANTIC


def is_unusable(output: Any) -> bool:
    """True when *output* carries no usable result (error or empty)."""
    if not isinstance(output, dict) or not output:
        return True
    return policy.output_has_error(output) or policy.output_is_empty(output)


def failure_reason(output: Any) -> str:
    """Short human-readable reason why *output* is unusable."""
    error = policy.first_error(output)
    if error:
        return error.strip()[:300]
    if not isinstance(output, dict) or not output:
        return "worker 没有返回结果"
    for key, value in output.items():
        if key.startswith("_") or key in ("messages", "tasks"):
            continue
        if isinstance(value, (list, dict)) and not value:
            return f"{key} 为空"
    return "产出为空或不可用"


def output_carries_result(output: Any) -> bool:
    return not is_unusable(output)


# ── retry ──────────────────────────────────────────────────────────────


def retry_task(task: dict, attempt: int, reason: str, *, transient: bool) -> dict:
    """Return a copy of *task* with retry feedback appended to its prompt.

    Workers read ``task['prompt']``, so appending the failure explanation makes
    every existing worker self-correcting without touching their code.
    """
    from backend.prompts import (
        TASK_RETRY_FEEDBACK_SEMANTIC,
        TASK_RETRY_FEEDBACK_TRANSIENT,
    )

    template = TASK_RETRY_FEEDBACK_TRANSIENT if transient else TASK_RETRY_FEEDBACK_SEMANTIC
    feedback = template.format(attempt=attempt + 1, reason=reason or "未知原因")

    retried = dict(task)
    retried["prompt"] = f"{task.get('prompt', '')}{feedback}"
    retried["_retry_of"] = task.get("id", "")
    retried["_attempt"] = attempt + 1
    retried["_retry_reason"] = reason
    retried["_retry_transient"] = transient
    return retried


# ── incremental re-planning ────────────────────────────────────────────


def _agent_lines(catalog: dict[str, dict]) -> str:
    lines = []
    for name, entry in sorted(catalog.items()):
        if not entry.get("has_worker", True):
            continue
        deps = entry.get("depends_on") or []
        suffix = f"（依赖 {', '.join(deps)}）" if deps else ""
        lines.append(f"- {name}: {entry.get('description', '')}{suffix}")
    return "\n".join(lines) or "- research: 检索"


def _attempt_digest(tasks: list[dict], state: dict, catalog: dict[str, dict]) -> str:
    lines: list[str] = []
    for task in tasks:
        agent = task.get("agent", "")
        entries = _agent_entries(state, agent, catalog)
        failed = [e for e in entries if policy.output_has_error(e)]
        if failed:
            status = f"失败（{policy.first_error(failed[0])[:140]}）"
        elif entries:
            status = f"成功（{len(entries)} 条结果）"
        else:
            status = "没有产出"
        lines.append(f"- [{agent}] {(task.get('prompt') or '')[:120]} → {status}")
    return "\n".join(lines) or "（无步骤）"


def _agent_entries(state: dict, agent: str, catalog: dict[str, dict]) -> list:
    key = (catalog.get(agent) or {}).get("result_key") or f"{agent}_results"
    if key == "skill_outputs":
        outputs = state.get("skill_outputs") or {}
        return list(outputs.get(agent) or []) if isinstance(outputs, dict) else []
    value = state.get(key)
    return list(value) if isinstance(value, list) else ([value] if value else [])


def _artifacts_digest(state: dict) -> str:
    from backend.safety.verifier import artifact_snapshot

    snapshot = artifact_snapshot(state)
    parts = [f"{k}={v}" for k, v in snapshot.items() if v]
    return "、".join(parts) if parts else "（无产出物）"


def _gap_digest(verdict: Optional[dict]) -> str:
    if not verdict:
        return "（未执行目标确认）"
    gaps = [f"{c.get('label')} — {c.get('evidence', '')}" for c in verdict.get("unmet", [])]
    llm = verdict.get("llm") or {}
    gaps.extend(llm.get("missing") or [])
    return "\n".join(f"- {g}" for g in gaps[:8]) or "（无明显缺口）"


def _parse_plan(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def replan(
    *,
    user_request: str,
    tasks: list[dict],
    state: dict,
    verdict: Optional[dict],
    catalog: dict[str, dict],
    model_id: str = "",
    max_steps: int = _MAX_STEPS,
    timeout: float = 45.0,
) -> tuple[list[dict], str]:
    """Ask the supervisor for a revised set of *remaining* steps.

    Returns ``(tasks, reason)``; an empty list means "nothing left to do" or
    "the planner was unavailable", in which case the caller falls back to the
    mechanical remediation built from the unmet criteria.
    """
    from backend.models.provider import create_chat_model
    from backend.prompts import REPLAN_PROMPT

    prompt = REPLAN_PROMPT.format(
        user_request=(user_request or "")[:800],
        attempts=_attempt_digest(tasks, state, catalog),
        artifacts=_artifacts_digest(state),
        gaps=_gap_digest(verdict),
        agents=_agent_lines(catalog),
        max_steps=max_steps,
    )

    try:
        llm = create_chat_model(
            model_id=model_id or "deepseek-ai/DeepSeek-V3", temperature=0.2, max_tokens=900
        )
        response = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=prompt)]), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning("Re-plan timed out after %.0fs", timeout)
        return [], ""
    except Exception as exc:
        logger.warning("Re-plan unavailable: %s", exc)
        return [], ""

    content = response.content if hasattr(response, "content") else str(response)
    data = _parse_plan(content)
    if not data:
        return [], ""

    known = set(catalog)
    planned: list[dict] = []
    for step in (data.get("plan") or [])[:max_steps]:
        if not isinstance(step, dict):
            continue
        agent = str(step.get("agent") or "").strip()
        if agent not in known:
            logger.info("Re-plan proposed unknown agent %r — dropped", agent)
            continue
        entry = catalog.get(agent) or {}
        if entry.get("has_worker") is False:
            continue
        planned.append({
            "agent": agent,
            "prompt": str(step.get("prompt") or "")[:600],
            "depends_on": step.get("depends_on") or [],
            "_replanned": True,
        })

    reason = str(data.get("reason") or "")[:300]
    return planned, reason
