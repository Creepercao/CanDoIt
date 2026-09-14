"""Execution policy engine — plan review, action gate, output review.

Three checkpoints guard one run:

``review_plan``   before any worker starts: is this plan safe and coherent?
``review_action`` immediately before one task executes: may it run?
``review_output`` right after one task returns: is the result trustworthy?

Each returns a :class:`PolicyDecision` carrying a risk level, the findings that
produced it, and the resulting decision.  Risk is mapped to a decision by the
configured ``SAFETY_MODE``:

===========  =========  =====================================
risk         decision   meaning
===========  =========  =====================================
low          allow      run silently
medium       notify     run, record and surface a note
high         review     stop and ask a human
critical     block      refuse
===========  =========  =====================================

``monitor`` mode downgrades every decision to ``allow`` while still recording
the intended decision, so operators can roll out enforcement safely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from backend.safety import injection
from backend.safety.redact import redact, scan_secrets
from backend.safety.settings import get_safety_settings

# ── decision vocabulary ────────────────────────────────────────────────

ALLOW = "allow"
NOTIFY = "notify"
REVIEW = "review"
BLOCK = "block"

_RISK_TO_DECISION = {
    "low": ALLOW,
    "medium": NOTIFY,
    "high": REVIEW,
    "critical": BLOCK,
}

# ── capability risk table ──────────────────────────────────────────────
# Built-in workers. Installed skill packages and MCP tools default to
# ``medium`` because they are third-party extensions.

AGENT_RISK: dict[str, tuple[str, str]] = {
    "research": ("low", "网络只读检索与抓取"),
    "analyst": ("low", "本地文本结构化提取"),
    "chart": ("low", "本地图表渲染"),
    "image_gen": ("low", "图像生成（外部模型调用）"),
    "video_gen": ("low", "视频生成（外部模型调用）"),
    "code": ("medium", "生成可执行代码（仅生成，不在本机执行）"),
    "ppt_planner": ("low", "幻灯片大纲规划"),
    "ppt_slide": ("low", "单页 HTML 幻灯片生成"),
    "ppt_assembler": ("low", "幻灯片组装"),
    "translator": ("low", "文本翻译"),
}

DEFAULT_AGENT_RISK = ("medium", "第三方扩展代理")

# ── policy-only content rules (injection rules cover the rest) ─────────

_POLICY_RULES: list[tuple[str, str, str, str, re.Pattern[str]]] = [
    (
        "policy.human_likeness",
        "content_safety",
        "high",
        "涉及真人肖像/深度伪造/成人内容的生成请求",
        re.compile(
            r"(?i)(deepfake|face[\s_-]?swap|换脸|真人(?:肖像|照片)|明星(?:脸|换脸)|"
            r"裸露|色情|nude|nsfw|未成年)"
        ),
    ),
    (
        "policy.mass_harvest",
        "data_harvesting",
        "medium",
        "大规模抓取/爬取，可能违反目标站点条款或造成压力",
        re.compile(
            r"(?i)(爬取全站|crawl\s+(?:the\s+)?(?:entire|whole)\s+site|批量抓取|"
            r"scrape\s+all\s+pages|遍历所有页面|\d{3,}\s*(?:个)?页面)"
        ),
    ),
    (
        "policy.credential_harvest",
        "credential_access",
        "critical",
        "要求读取/回显本机或项目凭据",
        re.compile(
            r"(?i)(读取|打印|输出|显示|回显|cat|dump|show)\s*[^\n]{0,40}"
            r"(API\.json|mcp_servers\.json|\.env\b|id_rsa|凭据|密钥文件|api\s*key)"
        ),
    ),
    (
        "policy.shell_execution",
        "code_execution",
        "high",
        "要求在本机执行 shell/系统命令",
        re.compile(
            r"(?i)(执行|运行|run|execute)\s*[^\n]{0,30}"
            r"(shell|bash|cmd|powershell|终端|命令行|系统命令|script)"
        ),
    ),
]

_CONTENT_CATEGORIES = {"content_safety", "data_harvesting"}


# ── data structures ────────────────────────────────────────────────────


@dataclass
class PolicyDecision:
    """Outcome of one policy checkpoint."""

    stage: str
    decision: str = ALLOW
    risk: str = "low"
    findings: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    summary: str = ""
    intended_decision: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def requires_approval(self) -> bool:
        return self.decision == REVIEW

    @property
    def blocked(self) -> bool:
        return self.decision == BLOCK

    @property
    def allowed(self) -> bool:
        return self.decision in (ALLOW, NOTIFY)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "decision": self.decision,
            "intended_decision": self.intended_decision or self.decision,
            "risk": self.risk,
            "summary": self.summary,
            "reasons": self.reasons,
            "findings": self.findings[:20],
            "meta": self.meta,
        }


# ── helpers ────────────────────────────────────────────────────────────


def classify_agent(agent: str, catalog: Optional[dict[str, dict]] = None) -> tuple[str, str]:
    """Return ``(risk, description)`` for an agent type."""
    if catalog and agent in catalog:
        entry = catalog[agent] or {}
        return entry.get("risk", DEFAULT_AGENT_RISK[0]), entry.get(
            "description", DEFAULT_AGENT_RISK[1]
        )
    return AGENT_RISK.get(agent, DEFAULT_AGENT_RISK)


def agent_executes(agent: str, catalog: Optional[dict[str, dict]] = None) -> bool:
    """True when the agent may act on the host rather than only produce content.

    Built-in workers call models and render files; installed skill packages and
    MCP tools are third-party code paths that may touch the host, so they are
    treated as executing unless the catalog says otherwise.  Unknown agents
    default to ``True`` — we know nothing about them.
    """
    if catalog and agent in catalog:
        return bool((catalog[agent] or {}).get("executes", True))
    return True


def _prompt_downgrade(agent: str, catalog: Optional[dict[str, dict]]) -> int:
    """Findings inside a content-only agent's prompt are one step less severe."""
    return 0 if agent_executes(agent, catalog) else 1


def _policy_findings(text: str, source: str, *, downgrade_steps: int = 0) -> list[dict]:
    findings: list[dict] = []
    for rule_id, category, severity, message, pattern in _POLICY_RULES:
        for match in pattern.finditer(text or ""):
            findings.append({
                "id": rule_id,
                "category": category,
                "severity": injection.downgrade(severity, downgrade_steps),
                "message": message,
                "evidence": match.group().strip().replace("\n", " ")[:120],
                "source": source,
                "offset": match.start(),
            })
    return findings


def _apply_mode(risk: str, findings: list[dict]) -> tuple[str, str]:
    """Map risk to a decision honouring ``SAFETY_MODE``."""
    settings = get_safety_settings()
    intended = _RISK_TO_DECISION.get(risk, ALLOW)
    if settings.mode == "off":
        return ALLOW, ALLOW
    if settings.mode == "monitor":
        return ALLOW, intended
    return intended, intended


def _risk_of(findings: Iterable[dict], floor: str = "low") -> str:
    severities = [f.get("severity", "low") for f in findings]
    severities.append(floor)
    return injection.max_severity(severities)


def _dedupe(findings: Iterable[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for item in findings:
        key = (item.get("id", ""), item.get("evidence", "")[:60])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _reasons(findings: list[dict], limit: int = 6) -> list[str]:
    reasons: list[str] = []
    ordered = sorted(
        findings, key=lambda f: injection.severity_rank(f.get("severity", "low")), reverse=True
    )
    for item in ordered:
        line = f"[{item.get('severity')}] {item.get('message')}"
        evidence = item.get("evidence")
        if evidence:
            line += f" — {evidence}"
        if line not in reasons:
            reasons.append(line)
        if len(reasons) >= limit:
            break
    return reasons


def _finalize(
    stage: str,
    findings: list[dict],
    *,
    floor_risk: str = "low",
    meta: Optional[dict] = None,
    summary: str = "",
) -> PolicyDecision:
    findings = _dedupe(findings)
    risk = _risk_of(findings, floor_risk)
    decision, intended = _apply_mode(risk, findings)
    decision_obj = PolicyDecision(
        stage=stage,
        decision=decision,
        intended_decision=intended,
        risk=risk,
        findings=findings,
        reasons=_reasons(findings),
        summary=summary or injection.summarize(findings),
        meta=meta or {},
    )
    return decision_obj


# ── checkpoints ────────────────────────────────────────────────────────


def review_plan(
    user_request: str,
    tasks: list[dict],
    *,
    catalog: Optional[dict[str, dict]] = None,
    max_steps: int = 32,
) -> PolicyDecision:
    """Pre-execution review of the supervisor's plan."""
    findings: list[dict] = []

    # The user's own request is lower-trust than the system prompt but higher
    # than scraped content — downgrade one step so a pasted document quoting
    # "rm -rf /" is flagged for review rather than blocked outright.
    findings.extend(
        injection.screen(user_request, source="user_request", downgrade_steps=1)
    )
    findings.extend(_policy_findings(user_request, "user_request", downgrade_steps=1))

    unknown: list[str] = []
    for task in tasks:
        agent = task.get("agent", "") or "?"
        if catalog is not None and agent not in catalog:
            unknown.append(agent)
        risk, description = classify_agent(agent, catalog)
        if risk in ("high", "critical"):
            findings.append({
                "id": "plan.high_risk_agent",
                "category": "capability",
                "severity": risk,
                "message": f"计划包含高风险代理 {agent}（{description}）",
                "evidence": (task.get("prompt") or "")[:120],
                "source": f"plan:{agent}",
            })
        # Screen each planned step as well: a destructive step must stop the
        # plan, not just the single task that would run it.
        steps = _prompt_downgrade(agent, catalog)
        findings.extend(
            injection.screen(task.get("prompt", "") or "", source=f"plan:{agent}",
                             downgrade_steps=steps)
        )
        findings.extend(
            _policy_findings(task.get("prompt", "") or "", f"plan:{agent}",
                             downgrade_steps=steps)
        )

    if unknown:
        # Unknown agents are dropped before execution (see
        # ``SafetyReviewer.screen_tasks``), so this is a repair notice rather
        # than a reason to stop the whole plan.
        findings.append({
            "id": "plan.unknown_agent",
            "category": "plan_integrity",
            "severity": "medium",
            "message": "计划引用了未注册的代理，这些步骤会被自动丢弃",
            "evidence": ", ".join(sorted(set(unknown))[:8]),
            "source": "plan",
        })

    if len(tasks) > max_steps:
        findings.append({
            "id": "plan.over_budget",
            "category": "budget",
            "severity": "high",
            "message": f"计划步骤数 {len(tasks)} 超过单次运行上限 {max_steps}",
            "evidence": f"{len(tasks)} > {max_steps}",
            "source": "plan",
        })

    return _finalize(
        "plan",
        findings,
        meta={"tasks": len(tasks), "unknown_agents": sorted(set(unknown))},
        summary=f"审查 {len(tasks)} 个计划步骤",
    )


def review_action(
    task: dict,
    *,
    catalog: Optional[dict[str, dict]] = None,
) -> PolicyDecision:
    """Pre-execution gate for a single task."""
    agent = task.get("agent", "") or "?"
    prompt = task.get("prompt", "") or ""
    risk, description = classify_agent(agent, catalog)

    findings: list[dict] = []
    if risk == "medium":
        findings.append({
            "id": "action.extension_agent",
            "category": "capability",
            "severity": "medium",
            "message": f"代理 {agent} 属于扩展能力（{description}）",
            "evidence": prompt[:120],
            "source": f"action:{agent}",
        })
    elif risk in ("high", "critical"):
        findings.append({
            "id": "action.high_risk_agent",
            "category": "capability",
            "severity": risk,
            "message": f"代理 {agent} 风险等级为 {risk}（{description}）",
            "evidence": prompt[:120],
            "source": f"action:{agent}",
        })

    findings.extend(
        injection.screen(prompt, source=f"task:{agent}",
                         downgrade_steps=_prompt_downgrade(agent, catalog))
    )
    findings.extend(
        _policy_findings(prompt, f"task:{agent}",
                         downgrade_steps=_prompt_downgrade(agent, catalog))
    )

    return _finalize(
        "action",
        findings,
        floor_risk=risk,
        meta={"agent": agent, "task_id": task.get("id", "")},
        summary=f"审查任务 {task.get('id') or agent}",
    )


def review_output(
    agent: str,
    task: dict,
    output: Any,
    *,
    text: str = "",
) -> PolicyDecision:
    """Post-execution review of one worker result."""
    findings: list[dict] = []

    if text:
        findings.extend(scan_secrets(text))
        findings.extend(
            injection.screen(text, source=f"output:{agent}", downgrade_steps=1)
        )

    if _has_error(output):
        findings.append({
            "id": "output.error",
            "category": "reliability",
            "severity": "medium",
            "message": f"代理 {agent} 返回了错误结果",
            "evidence": _first_error(output)[:200],
            "source": f"output:{agent}",
        })

    if _is_empty(output):
        findings.append({
            "id": "output.empty",
            "category": "reliability",
            "severity": "medium",
            "message": f"代理 {agent} 没有产出任何结果",
            "evidence": (task.get("prompt") or "")[:120],
            "source": f"output:{agent}",
        })

    return _finalize(
        "output",
        findings,
        meta={"agent": agent, "task_id": task.get("id", "")},
        summary=f"审查 {agent} 的输出",
    )


def redact_output(output: Any) -> tuple[Any, int]:
    """Recursively redact credentials inside a worker result.

    Returns ``(sanitized_output, redaction_count)``; the count is derived by
    comparing serialized lengths so callers can audit the action.
    """
    before = _stringify(output)
    sanitized = redact(output)
    after = _stringify(sanitized)
    changed = before != after
    return sanitized, (1 if changed else 0)


# Public predicates over worker results, shared with the recovery layer.

def output_has_error(output: Any) -> bool:
    """True when any nested entry of *output* carries an ``error`` field."""
    return _has_error(output)


def output_is_empty(output: Any) -> bool:
    """True when *output* contains no meaningful result payload."""
    return _is_empty(output)


def first_error(output: Any) -> str:
    """First ``error`` message found in *output* (empty string when none)."""
    return _first_error(output)


# ── output helpers ─────────────────────────────────────────────────────


def _stringify(value: Any, limit: int = 200_000) -> str:
    import json

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit]


def _walk(value: Any, depth: int = 0):
    if depth > 6:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item, depth + 1)


def _has_error(output: Any) -> bool:
    for node in _walk(output):
        if isinstance(node, dict):
            if node.get("error"):
                return True
            result = node.get("result")
            if isinstance(result, dict) and result.get("error"):
                return True
    return False


def _first_error(output: Any) -> str:
    for node in _walk(output):
        if isinstance(node, dict):
            if node.get("error"):
                return str(node["error"])
            result = node.get("result")
            if isinstance(result, dict) and result.get("error"):
                return str(result["error"])
    return ""


def _is_empty(output: Any) -> bool:
    if output is None:
        return True
    if isinstance(output, dict):
        if not output:
            return True
        meaningful = [
            v for k, v in output.items()
            if k not in ("messages", "_add_tasks") and not k.startswith("_")
        ]
        return not any(bool(v) for v in meaningful)
    if isinstance(output, (list, tuple, str)):
        return len(output) == 0
    return False


def aggregate(decisions: Iterable[PolicyDecision]) -> dict:
    """Collapse many decisions into a run-level safety summary."""
    items = list(decisions)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.decision] = counts.get(item.decision, 0) + 1
    return {
        "checkpoints": len(items),
        "decisions": counts,
        "risk": injection.max_severity([d.risk for d in items]),
        "blocked": sum(1 for d in items if d.blocked),
        "reviews": sum(1 for d in items if d.requires_approval),
        "findings": sum(len(d.findings) for d in items),
    }
