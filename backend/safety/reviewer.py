"""SafetyReviewer — the facade the orchestrator talks to.

One instance guards one run.  It owns the run budget, the catalog of known
agents, the audit records, and the checkpoints themselves, so the orchestration
loop only has to ask three questions:

* ``review_plan``   — may this plan run?
* ``review_action`` — may this one task run?
* ``review_output`` — is this result acceptable?

plus the closing ``verify`` step that confirms the user's goal was actually met
and, when it was not, proposes bounded remediation tasks.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.safety import policy, verifier
from backend.safety.approvals import ApprovalRequest, get_approval_store
from backend.safety.audit import get_audit_log
from backend.safety.budget import RunBudget
from backend.safety.injection import screen, wrap_untrusted
from backend.safety.policy import PolicyDecision
from backend.safety.settings import get_safety_settings

logger = logging.getLogger("safety.reviewer")

MAX_REMEDIATION_TASKS = 3


def build_agent_catalog() -> dict[str, dict]:
    """Describe every enabled agent for the policy engine and verifier."""
    catalog: dict[str, dict] = {}
    try:
        from backend.skills.registry import skill_registry

        for name, skill in skill_registry.get_enabled().items():
            risk, description = policy.classify_agent(name)
            is_package = skill._package_meta is not None
            is_mcp = bool(skill.tools) and not is_package
            catalog[name] = {
                "display_name": skill.display_name,
                "description": skill.description or description,
                "risk": risk,
                "result_key": skill.result_key or f"{name}_results",
                "depends_on": list(skill.depends_on or []),
                "is_package": is_package,
                "kind": "package" if is_package else ("mcp" if is_mcp else "builtin"),
                # Third-party extensions can reach the host; built-ins cannot.
                "executes": is_package or is_mcp,
                "has_worker": skill.worker is not None or skill_registry._has_worker(skill),
                "emoji": skill.emoji,
            }
    except Exception as exc:  # registry not ready (tests, CLI use)
        logger.debug("Agent catalog built without registry: %s", exc)

    for name in policy.AGENT_RISK:
        catalog.setdefault(name, {
            "display_name": name,
            "description": policy.AGENT_RISK[name][1],
            "risk": policy.AGENT_RISK[name][0],
            "result_key": f"{name}_results",
            "depends_on": [],
            "is_package": False,
            "kind": "builtin",
            "executes": False,
            "has_worker": False,
            "emoji": "",
        })
    return catalog


class SafetyReviewer:
    """Per-run safety guard."""

    def __init__(
        self,
        run_id: str,
        *,
        session_id: str = "",
        streaming: bool = False,
        budget: Optional[RunBudget] = None,
        model_id: str = "",
        catalog: Optional[dict[str, dict]] = None,
    ):
        self.run_id = run_id
        self.session_id = session_id
        self.streaming = streaming
        self.budget = budget or RunBudget.from_env()
        self.model_id = model_id
        self.catalog = catalog if catalog is not None else build_agent_catalog()
        self.decisions: list[PolicyDecision] = []
        self.verdict: Optional[dict] = None
        self.redactions = 0
        self.dropped_tasks: list[dict] = []
        self._audit = get_audit_log()

    # ── audit ──────────────────────────────────────────────────────────

    def record(self, event: str, *, decision: str = "", risk: str = "",
               severity: str = "info", message: str = "", target: str = "",
               stage: str = "", details: Optional[dict] = None) -> dict:
        return self._audit.record(
            event,
            run_id=self.run_id,
            session_id=self.session_id,
            stage=stage,
            actor="safety",
            target=target,
            decision=decision,
            risk=risk,
            severity=severity,
            message=message,
            details=details,
        )

    def record_decision(self, decision: PolicyDecision, *, target: str = "") -> dict:
        self.decisions.append(decision)
        return self.record(
            f"review.{decision.stage}",
            stage=decision.stage,
            decision=decision.decision,
            risk=decision.risk,
            severity=_severity_for(decision),
            message=decision.summary,
            target=target or decision.meta.get("agent", ""),
            details=decision.to_dict(),
        )

    # ── catalog helpers ────────────────────────────────────────────────

    def known(self, agent: str) -> bool:
        return agent in self.catalog

    def entry(self, agent: str) -> dict:
        return self.catalog.get(agent) or {}

    # ── plan ───────────────────────────────────────────────────────────

    def screen_tasks(self, tasks: list[dict]) -> tuple[list[dict], list[dict]]:
        """Drop tasks that reference agents this deployment does not have.

        An unknown agent can never be executed; leaving it in the wave graph
        stalls the run until the deadlock guard fires.  Dropping it is an
        execution-review decision, so it is always audited.
        """
        kept: list[dict] = []
        dropped: list[dict] = []
        for task in tasks:
            agent = task.get("agent", "")
            if agent and agent in self.catalog:
                kept.append(task)
            else:
                dropped.append({"agent": agent, "prompt": (task.get("prompt") or "")[:200]})
        if dropped:
            logger.warning(
                "Run %s: dropped %d task(s) with unknown agents: %s",
                self.run_id, len(dropped), [d["agent"] for d in dropped],
            )
            self.record(
                "review.plan.task_dropped",
                stage="plan",
                decision="block",
                risk="high",
                severity="warning",
                message=f"丢弃了 {len(dropped)} 个引用未注册代理的计划步骤",
                details={"dropped": dropped, "kept": len(kept)},
            )
        self.dropped_tasks = dropped
        return kept, dropped

    def review_plan(self, user_request: str, tasks: list[dict]) -> PolicyDecision:
        decision = policy.review_plan(
            user_request, tasks, catalog=self.catalog, max_steps=self.budget.max_steps
        )
        return decision

    async def enrich_plan_review(
        self, user_request: str, tasks: list[dict], decision: PolicyDecision
    ) -> PolicyDecision:
        """Optionally let an LLM reviewer tighten the plan decision."""
        settings = get_safety_settings()
        if not settings.enabled or not settings.llm_review:
            return decision
        if not self.budget.allow_llm_review():
            decision.reasons.append("[info] 已跳过 LLM 计划评审（达到评审次数上限）")
            return decision
        if not _needs_llm_review(decision):
            return decision

        self.budget.consume_llm_review()
        review = await _llm_review_plan(
            user_request=user_request,
            tasks=tasks,
            decision=decision,
            model_id=self.model_id,
        )
        if review is None:
            decision.reasons.append("[info] LLM 计划评审不可用，仅依据静态审查结果")
            return decision

        verdict = review.get("verdict", "approve")
        risk = review.get("risk", decision.risk)
        decision.meta["llm_review"] = review
        for reason in review.get("reasons") or []:
            decision.reasons.append(f"[LLM] {reason}")

        # An LLM reviewer may tighten, never loosen: a human gate and a block
        # both survive an "approve" verdict.
        if verdict == "reject" and not decision.blocked:
            decision.decision = policy.BLOCK
            decision.intended_decision = policy.BLOCK
            decision.risk = "critical"
        elif verdict == "review" and decision.allowed:
            decision.decision = policy.REVIEW
            decision.intended_decision = policy.REVIEW
            decision.risk = risk if risk in ("high", "critical") else "high"
        elif risk in ("high", "critical") and decision.allowed and settings.mode == "enforce":
            decision.decision = policy.REVIEW if risk == "high" else policy.BLOCK
            decision.intended_decision = decision.decision
            decision.risk = risk
        return decision

    # ── action ─────────────────────────────────────────────────────────

    def review_action(self, task: dict) -> PolicyDecision:
        return policy.review_action(task, catalog=self.catalog)

    # ── output ─────────────────────────────────────────────────────────

    def review_output(self, task: dict, output: dict) -> PolicyDecision:
        text = policy._stringify(output, limit=60_000)
        return policy.review_output(task.get("agent", ""), task, output, text=text)

    def scrub_output(self, output: dict) -> tuple[dict, int]:
        """Redact credentials from a worker result before it enters state."""
        if not get_safety_settings().redact_outputs:
            return output, 0
        sanitized, flagged = policy.redact_output(output)
        if flagged:
            self.redactions += 1
            self.record(
                "guard.output_redacted",
                stage="output",
                decision="allow",
                risk="critical",
                severity="critical",
                message="worker 输出中检测到凭据，已脱敏后再进入状态",
                details={"keys": sorted(output.keys())},
            )
        return sanitized if isinstance(sanitized, dict) else output, flagged

    # ── untrusted content ──────────────────────────────────────────────

    def fence_untrusted(self, text: str, source: str) -> str:
        """Fence external text and audit any injection findings."""
        findings = screen(text, source=source)
        high = [f for f in findings if f.get("severity") in ("high", "critical")]
        if high:
            self.record(
                "guard.untrusted_content",
                stage="research",
                decision="notify",
                risk="high",
                severity="warning",
                message=f"外部内容包含 {len(high)} 条可疑注入特征（{source}），已按数据隔离",
                details={"findings": high[:10]},
            )
        return wrap_untrusted(text, source)

    # ── approvals ──────────────────────────────────────────────────────

    async def request_approval(
        self, decision: PolicyDecision, *, stage: str, target: str = "", task: Optional[dict] = None
    ) -> ApprovalRequest:
        """Create an approval request; auto-approve honours the operator switch."""
        store = get_approval_store()
        settings = get_safety_settings()
        request = await store.create(
            run_id=self.run_id,
            stage=stage,
            risk=decision.risk,
            summary=decision.summary,
            reasons=decision.reasons,
            findings=decision.findings[:10],
            agent=(task or {}).get("agent", "") or target,
            task=(task or {}).get("prompt", ""),
            session_id=self.session_id,
        )
        self.record(
            "approval.requested",
            stage=stage,
            decision="review",
            risk=decision.risk,
            severity="warning",
            message=decision.summary,
            target=target or request.agent,
            details={"approval_id": request.id, "reasons": decision.reasons},
        )
        if settings.auto_approve:
            return await store.decide_and_wait(request, True, decided_by="auto_approve")
        return request

    async def wait_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        if request.status != "pending":
            return request
        store = get_approval_store()
        resolved = await store.wait(request.id, timeout=get_safety_settings().approval_timeout)
        self.record(
            "approval.resolved",
            stage=request.stage,
            decision="allow" if resolved.status == "approved" else "block",
            risk=request.risk,
            severity="info" if resolved.status == "approved" else "warning",
            message=f"审批 {resolved.status}（{resolved.decided_by}）",
            target=request.agent,
            details={"approval_id": resolved.id, "note": resolved.note},
        )
        return resolved

    async def resolve_without_human(
        self, decision: PolicyDecision, *, stage: str, target: str = ""
    ) -> ApprovalRequest:
        """Non-streaming runs have no UI, so fall back to configuration."""
        settings = get_safety_settings()
        approved = settings.nonstream_review == "allow" or settings.auto_approve
        if settings.nonstream_review == "deny" and not settings.auto_approve:
            approved = False
        store = get_approval_store()
        request = await store.create(
            run_id=self.run_id,
            stage=stage,
            risk=decision.risk,
            summary=decision.summary,
            reasons=decision.reasons,
            findings=decision.findings[:10],
            agent=target,
            session_id=self.session_id,
        )
        resolved = await store.decide_and_wait(
            request, approved, decided_by=f"nonstream:{settings.nonstream_review}"
        )
        self.record(
            "approval.resolved",
            stage=stage,
            decision="allow" if approved else "block",
            risk=decision.risk,
            severity="warning",
            message=f"非流式运行按配置处理审批：{settings.nonstream_review}",
            target=target,
            details={"approval_id": resolved.id},
        )
        return resolved

    # ── goal verification ──────────────────────────────────────────────

    async def verify(
        self, user_request: str, tasks: list[dict], state: dict, *, final_response: str = ""
    ) -> dict:
        settings = get_safety_settings()
        use_llm = settings.enabled and settings.goal_verification and self.budget.allow_llm_review()
        if use_llm:
            self.budget.consume_llm_review()

        verdict = await verifier.verify_goal_completion(
            user_request=user_request,
            tasks=tasks,
            state=state,
            catalog=self.catalog,
            model_id=self.model_id,
            final_response=final_response,
            use_llm=use_llm,
        )
        verdict["run_id"] = self.run_id
        verdict["artifacts"] = verifier.artifact_snapshot(state)
        self.verdict = verdict

        severity = "info" if verdict["status"] == verifier.COMPLETE else "warning"
        self.record(
            "verification.goal",
            stage="verify",
            decision="allow" if verdict["status"] == verifier.COMPLETE else "review",
            risk="low" if verdict["status"] == verifier.COMPLETE else "medium",
            severity=severity,
            message=verdict.get("summary", ""),
            details={
                "status": verdict["status"],
                "score": verdict["score"],
                "unmet": [c.get("label") for c in verdict.get("unmet", [])],
                "checked_by": verdict.get("checked_by", []),
                "llm_error": verdict.get("llm_error", ""),
            },
        )
        return verdict

    def build_remediation(self, tasks: list[dict]) -> list[dict]:
        """Propose deterministic remediation tasks for an incomplete verdict.

        Pure proposal: the caller decides whether to run them and owns the
        re-plan budget/audit accounting, because an LLM re-plan may be
        attempted first and only one of the two strategies is used.
        """
        if not self.verdict or self.verdict["status"] == verifier.COMPLETE:
            return []
        if not get_safety_settings().auto_remediate:
            return []
        return verifier.build_remediation_tasks(
            self.verdict, tasks, self.catalog, max_tasks=MAX_REMEDIATION_TASKS
        )

    # ── summary ────────────────────────────────────────────────────────

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "mode": get_safety_settings().mode,
            "policy": policy.aggregate(self.decisions),
            "dropped_tasks": self.dropped_tasks,
            "redactions": self.redactions,
            "verification": self.verdict,
            "budget": self.budget.snapshot(),
        }


# ── helpers ────────────────────────────────────────────────────────────


def _severity_for(decision: PolicyDecision) -> str:
    if decision.decision == policy.BLOCK:
        return "critical"
    if decision.decision == policy.REVIEW:
        return "warning"
    if decision.risk == "medium":
        return "info"
    return "info"


def _needs_llm_review(decision: PolicyDecision) -> bool:
    """Only spend a model call when the static scan found something."""
    import os

    if os.environ.get("SAFETY_LLM_REVIEW_ALWAYS", "").strip().lower() in ("1", "true", "yes"):
        return True
    if decision.risk in ("high", "critical"):
        return True
    return any(
        f.get("severity") in ("high", "critical")
        or f.get("category") in ("plan_integrity", "prompt_injection", "capability")
        for f in decision.findings
    )


async def _llm_review_plan(
    *, user_request: str, tasks: list[dict], decision: PolicyDecision, model_id: str
) -> Optional[dict]:
    import asyncio
    import json
    import re

    from langchain_core.messages import HumanMessage

    from backend.prompts import PLAN_REVIEW_PROMPT

    plan_lines = "\n".join(
        f"{idx + 1}. [{t.get('agent')}] {(t.get('prompt') or '')[:200]}"
        for idx, t in enumerate(tasks)
    )
    finding_lines = "\n".join(
        f"- [{f.get('severity')}] {f.get('message')} — {f.get('evidence', '')}"
        for f in decision.findings[:12]
    ) or "（无）"

    prompt = PLAN_REVIEW_PROMPT.format(
        user_request=(user_request or "")[:800],
        plan=plan_lines or "（空计划）",
        findings=finding_lines,
    )

    try:
        from backend.models.provider import create_chat_model

        llm = create_chat_model(
            model_id=model_id or "deepseek-ai/DeepSeek-V3", temperature=0.0, max_tokens=600
        )
        response = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=prompt)]), timeout=40.0
        )
        content = response.content if hasattr(response, "content") else str(response)
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group())
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("LLM plan review unavailable: %s", exc)
        return None
