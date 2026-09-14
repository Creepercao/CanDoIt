"""Safety API — execution review status, audit trail, and approvals.

Exposes the ``backend.safety`` layer to the GUI:

* ``GET  /safety/status``            overall posture (mode, counters, budget)
* ``GET  /safety/policy``            agent risk catalog and active rules
* ``GET  /safety/audit``             filtered audit trail
* ``GET  /safety/audit/verify``      hash-chain integrity check
* ``GET  /safety/audit/{run_id}``    one run's full record
* ``GET  /safety/approvals``         pending + recent approval decisions
* ``POST /safety/approvals/{id}``    approve or reject a stopped action
* ``GET  /safety/settings``          runtime switches
* ``PATCH /safety/settings``         change mode / auto-approve / LLM judge
* ``POST /safety/review``            ad-hoc policy check for pasted text
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.safety import policy
from backend.safety.approvals import get_approval_store
from backend.safety.audit import get_audit_log
from backend.safety.budget import RunBudget
from backend.safety.injection import screen
from backend.safety.reviewer import build_agent_catalog
from backend.safety.settings import get_safety_settings

router = APIRouter(tags=["safety"])
logger = logging.getLogger("api.safety")


class ApprovalDecision(BaseModel):
    approved: bool
    note: str = ""
    decided_by: str = "user"


class SafetySettingsUpdate(BaseModel):
    mode: Optional[str] = None
    auto_approve: Optional[bool] = None
    llm_review: Optional[bool] = None
    goal_verification: Optional[bool] = None
    auto_remediate: Optional[bool] = None
    redact_outputs: Optional[bool] = None
    nonstream_review: Optional[str] = None
    approval_timeout: Optional[float] = None


class ReviewRequest(BaseModel):
    text: str
    stage: str = "action"          # "action" | "plan" | "content"
    agent: str = ""


# ── status & configuration ─────────────────────────────────────────────


@router.get("/safety/status")
async def safety_status():
    """Overall safety posture — safe to poll from the UI."""
    settings = get_safety_settings()
    audit = get_audit_log()
    approvals = get_approval_store()
    catalog = build_agent_catalog()
    return {
        "settings": settings.snapshot(),
        "audit": audit.stats(),
        "approvals": approvals.stats(),
        "budget": RunBudget.from_env().snapshot(),
        "agents": {
            "total": len(catalog),
            "by_risk": _count_by(catalog, "risk"),
            "executing": sum(1 for v in catalog.values() if v.get("executes")),
            "package": sum(1 for v in catalog.values() if v.get("is_package")),
        },
        "rules": {
            "policy": len(policy._POLICY_RULES),
            "static": len(_static_rule_ids()),
        },
    }


@router.get("/safety/policy")
async def safety_policy():
    """Agent risk classification plus the active content rules."""
    catalog = build_agent_catalog()
    return {
        "settings": get_safety_settings().snapshot(),
        "agents": [
            {"name": name, **{k: v for k, v in entry.items() if k != "emoji"}}
            for name, entry in sorted(catalog.items())
        ],
        "policy_rules": [
            {"id": rid, "category": cat, "severity": sev, "message": msg}
            for rid, cat, sev, msg, _pattern in policy._POLICY_RULES
        ],
        "injection_rules": _static_rule_ids(),
        "risk_matrix": {
            "low": policy.ALLOW,
            "medium": policy.NOTIFY,
            "high": policy.REVIEW,
            "critical": policy.BLOCK,
        },
    }


@router.get("/safety/settings")
async def get_settings():
    return get_safety_settings().snapshot()


@router.patch("/safety/settings")
async def update_settings(payload: SafetySettingsUpdate):
    changes = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not changes:
        return get_safety_settings().snapshot()
    if "mode" in changes and changes["mode"] not in ("off", "monitor", "enforce"):
        raise HTTPException(status_code=400, detail="mode 必须是 off / monitor / enforce")
    updated = get_safety_settings().update(**changes)
    get_audit_log().record(
        "settings.updated",
        actor="user",
        stage="settings",
        decision="allow",
        severity="warning",
        message="安全策略配置已更新",
        details=changes,
    )
    return updated


# ── audit trail ────────────────────────────────────────────────────────


@router.get("/safety/audit/verify")
async def verify_audit(day: str = ""):
    """Recompute the audit hash chain and report the first broken link."""
    return get_audit_log().verify_chain(day)


@router.get("/safety/audit")
async def list_audit(
    run_id: str = "",
    session_id: str = "",
    event: str = "",
    severity: str = "",
    decision: str = "",
    day: str = "",
    limit: int = 200,
):
    limit = max(1, min(limit, 1000))
    records = get_audit_log().query(
        run_id=run_id, session_id=session_id, event=event,
        severity=severity, decision=decision, day=day, limit=limit,
    )
    return {"records": records, "count": len(records), "days": get_audit_log().available_days()}


@router.get("/safety/audit/{run_id}")
async def get_run_audit(run_id: str, limit: int = 500):
    records = get_audit_log().query(run_id=run_id, limit=max(1, min(limit, 1000)))
    if not records:
        raise HTTPException(status_code=404, detail=f"未找到 run {run_id} 的审计记录")
    return {"run_id": run_id, "records": records, "count": len(records)}


# ── approvals ──────────────────────────────────────────────────────────


@router.get("/safety/approvals")
async def list_approvals(limit: int = 50):
    store = get_approval_store()
    return {
        "pending": [r.to_dict() for r in store.pending()],
        "history": [r.to_dict() for r in store.history(limit=max(1, min(limit, 200)))],
        "stats": store.stats(),
    }


@router.get("/safety/approvals/{approval_id}")
async def get_approval(approval_id: str):
    request = get_approval_store().find(approval_id)
    if request is None:
        raise HTTPException(status_code=404, detail="审批请求不存在或已过期")
    return request.to_dict()


@router.post("/safety/approvals/{approval_id}")
async def decide_approval(approval_id: str, payload: ApprovalDecision):
    """Approve or reject a stopped action. The waiting run resumes immediately."""
    store = get_approval_store()
    request = store.get(approval_id)
    if request is None:
        existing = store.find(approval_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="审批请求不存在或已过期")
        return {"ok": False, "already_decided": True, "request": existing.to_dict()}

    resolved = await store.decide(
        approval_id,
        payload.approved,
        decided_by=payload.decided_by or "user",
        note=payload.note,
    )
    get_audit_log().record(
        "approval.decided",
        run_id=request.run_id,
        session_id=request.session_id,
        stage=request.stage,
        actor=payload.decided_by or "user",
        target=request.agent,
        decision="allow" if payload.approved else "block",
        risk=request.risk,
        severity="warning",
        message=f"人工审批{'通过' if payload.approved else '拒绝'}：{request.summary}",
        details={"approval_id": approval_id, "note": payload.note},
    )
    return {"ok": True, "request": resolved.to_dict() if resolved else None}


# ── ad-hoc review ──────────────────────────────────────────────────────


@router.post("/safety/review")
async def review_text(payload: ReviewRequest):
    """Run the deterministic policy engine over arbitrary text.

    Useful for inspecting why a plan or a prompt would be flagged, without
    executing anything.
    """
    catalog = build_agent_catalog()
    if payload.stage == "plan":
        tasks = [{"agent": payload.agent or "research", "prompt": payload.text}]
        decision = policy.review_plan(payload.text, tasks, catalog=catalog)
    elif payload.stage == "content":
        findings = screen(payload.text, source="manual")
        risk = max(
            (f.get("severity", "low") for f in findings),
            key=policy.injection.severity_rank,
            default="low",
        )
        mapped, intended = policy._apply_mode(risk, findings)
        decision = policy.PolicyDecision(
            stage="content",
            decision=mapped,
            intended_decision=intended,
            risk=risk,
            findings=findings,
            reasons=policy._reasons(findings),
            summary=policy.injection.summarize(findings),
        )
    else:
        task = {"agent": payload.agent or "code", "prompt": payload.text, "id": "manual:1"}
        decision = policy.review_action(task, catalog=catalog)

    return {
        "ok": True,
        "decision": decision.to_dict(),
        "risk": decision.risk,
        "findings": decision.findings,
    }


# ── helpers ────────────────────────────────────────────────────────────


def _count_by(catalog: dict[str, dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in catalog.values():
        value = str(entry.get(key, ""))
        counts[value] = counts.get(value, 0) + 1
    return counts


def _static_rule_ids() -> list[dict]:
    from backend.safety.injection import _RULES

    return [
        {"id": rid, "category": cat, "severity": sev, "message": msg}
        for rid, cat, sev, msg, _pattern in _RULES
    ]
