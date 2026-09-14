"""Goal completion confirmation.

A run is only "done" when the artifacts prove it.  ``verify_goal_completion``
therefore checks two independent things:

1. **Deterministic evidence** — every planned step produced a non-error result,
   and every deliverable the user explicitly asked for actually exists in the
   run state (chart URL, image URL, HTML deck, research sources, data table…).
2. **LLM judge** — a separate model call confirms the artifacts really answer
   the request, guards against "promised but not delivered" replies, and may
   surface unverified claims.

Both verdicts are reported.  The deterministic pass never depends on a model,
so a provider outage degrades judgement, not correctness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Optional

from langchain_core.messages import HumanMessage

from backend.prompts import GOAL_VERIFICATION_PROMPT

logger = logging.getLogger("safety.verifier")

LLM_VERIFY_TIMEOUT = 45.0

COMPLETE = "complete"
PARTIAL = "partial"
INCOMPLETE = "incomplete"
FAILED = "failed"

HTML_SKILL_AGENTS = {"ppt_planner", "ppt_slide", "ppt_assembler", "ppt-animation", "学霸笔记"}


# ── state readers ──────────────────────────────────────────────────────


def result_key_for(agent: str, catalog: Optional[dict[str, dict]] = None) -> str:
    entry = (catalog or {}).get(agent) or {}
    return entry.get("result_key") or f"{agent}_results"


def agent_entries(state: dict, agent: str, catalog: Optional[dict[str, dict]] = None) -> list[Any]:
    """Return every result entry produced for *agent*."""
    key = result_key_for(agent, catalog)
    if key == "skill_outputs":
        outputs = state.get("skill_outputs") or {}
        if not isinstance(outputs, dict):
            return []
        if agent in outputs:
            return list(outputs.get(agent) or [])
        # Skill package agents write under their own name; MCP tools too.
        return list(outputs.get(agent.replace("_worker", "")) or [])
    value = state.get(key)
    if isinstance(value, list):
        return list(value)
    if value:
        return [value]
    return []


def _entry_failed(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("error"):
        return True
    result = entry.get("result")
    if isinstance(result, dict) and result.get("error"):
        return True
    return False


def _entry_url(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    result = entry.get("result")
    if isinstance(result, dict):
        return result.get("url") or result.get("video_url") or ""
    return entry.get("url") or ""


# ── artifact checkers ──────────────────────────────────────────────────


def _has_url(entries: list[Any]) -> tuple[bool, str]:
    urls = [u for u in (_entry_url(e) for e in entries) if u]
    return (bool(urls), urls[0] if urls else "")


def _check_chart(state: dict, catalog) -> tuple[bool, str]:
    ok, url = _has_url(agent_entries(state, "chart", catalog))
    return ok, url or "没有任何图表文件生成"


def _check_image(state: dict, catalog) -> tuple[bool, str]:
    ok, url = _has_url(agent_entries(state, "image_gen", catalog))
    return ok, url or "没有任何图片文件生成"


def _check_video(state: dict, catalog) -> tuple[bool, str]:
    ok, url = _has_url(agent_entries(state, "video_gen", catalog))
    return ok, url or "没有任何视频文件生成"


def _check_code(state: dict, catalog) -> tuple[bool, str]:
    entries = agent_entries(state, "code", catalog)
    usable = [e for e in entries if not _entry_failed(e)]
    text = ""
    for entry in usable:
        if isinstance(entry, dict):
            text = entry.get("code") or entry.get("result") or ""
        elif isinstance(entry, str):
            text = entry
        if isinstance(text, str) and text.strip():
            return True, f"{len(text)} 字符代码"
    return False, "没有可用代码产出"


def _html_artifacts(state: dict) -> list[dict]:
    artifacts: list[dict] = []
    outputs = state.get("skill_outputs") or {}
    if not isinstance(outputs, dict):
        return artifacts
    for skill_name, items in outputs.items():
        for item in items or []:
            if isinstance(item, dict) and item.get("html_url"):
                artifacts.append({
                    "skill": skill_name,
                    "url": item["html_url"],
                    "title": item.get("html_title", ""),
                })
    return artifacts


def _check_html(state: dict, catalog) -> tuple[bool, str]:
    artifacts = _html_artifacts(state)
    if artifacts:
        return True, artifacts[0]["url"]
    return False, "没有生成 HTML 交付物"


def _check_research(state: dict, catalog) -> tuple[bool, str]:
    entries = agent_entries(state, "research", catalog)
    for entry in entries:
        if not isinstance(entry, dict) or _entry_failed(entry):
            continue
        synthesis = entry.get("synthesis") or ""
        sources = entry.get("sources") or []
        if isinstance(synthesis, str) and len(synthesis.strip()) >= 50:
            return True, f"{len(sources)} 个来源，综述 {len(synthesis)} 字符"
    return False, "没有有效的检索结果"


def _check_data_table(state: dict, catalog) -> tuple[bool, str]:
    for entry in agent_entries(state, "chart", catalog):
        if isinstance(entry, dict) and entry.get("table_markdown"):
            return True, "已生成 Markdown 数据表格"
    return False, "没有结构化数据表格"


# ── deliverable rules ──────────────────────────────────────────────────

DELIVERABLE_RULES: list[dict] = [
    {
        "id": "research",
        "label": "联网检索依据",
        "keywords": ["搜索", "查一下", "查一查", "最新", "资料", "调研", "研究报告",
                     "数据来源", "search", "latest", "research"],
        "check": _check_research,
        "agent": "research",
    },
    {
        "id": "data_table",
        "label": "结构化数据表格",
        "keywords": ["表格", "统计", "排名", "对比", "占比", "table"],
        "check": _check_data_table,
        "agent": "analyst",
    },
    {
        "id": "chart",
        "label": "数据图表",
        "keywords": ["图表", "柱状图", "折线图", "饼图", "散点图", "可视化", "chart", "graph"],
        "check": _check_chart,
        "agent": "chart",
    },
    {
        "id": "image",
        "label": "图片",
        "keywords": ["图片", "插画", "海报", "配图", "画一张", "画个", "生成图片",
                     "头像", "logo", "image", "illustration"],
        "check": _check_image,
        "agent": "image_gen",
    },
    {
        "id": "video",
        "label": "视频",
        "keywords": ["视频", "动画视频", "video", "animation"],
        "check": _check_video,
        "agent": "video_gen",
    },
    {
        "id": "code",
        "label": "代码/脚本",
        "keywords": ["代码", "脚本", "程序", "函数", "code", "script", "html 页面"],
        "check": _check_code,
        "agent": "code",
    },
    {
        "id": "html_deck",
        "label": "HTML 交付物（PPT/笔记等）",
        "keywords": ["ppt", "幻灯片", "演示文稿", "演示", "slide", "presentation",
                     "笔记", "学霸笔记"],
        "check": _check_html,
        "agent": "ppt_planner",
    },
]


def _requested_rules(user_request: str) -> list[dict]:
    text = (user_request or "").lower()
    return [rule for rule in DELIVERABLE_RULES if any(k in text for k in rule["keywords"])]


# ── deterministic checks ───────────────────────────────────────────────


def _step_criterion(agent: str, state: dict, catalog: Optional[dict[str, dict]]) -> dict:
    entries = agent_entries(state, agent, catalog)
    usable = [e for e in entries if not _entry_failed(e)]
    failed = [e for e in entries if _entry_failed(e)]
    if usable:
        status, evidence = "met", f"{len(usable)} 条结果"
    elif failed:
        status, evidence = "partial", f"{len(failed)} 条结果全部报错"
    else:
        status, evidence = "unmet", "没有产生任何结果"
    label = ((catalog or {}).get(agent) or {}).get("display_name") or agent
    return {
        "id": f"step:{agent}",
        "label": f"步骤产出 · {label}",
        "kind": "step",
        "agent": agent,
        "required": True,
        "weight": 1.0,
        "status": status,
        "evidence": evidence,
    }


def deterministic_checks(
    user_request: str,
    tasks: list[dict],
    state: dict,
    catalog: Optional[dict[str, dict]] = None,
) -> list[dict]:
    """Build the deterministic criterion list for one run."""
    criteria: list[dict] = []

    agents: list[str] = []
    for task in tasks:
        agent = task.get("agent", "")
        if agent and agent not in agents:
            agents.append(agent)
    for agent in agents:
        criteria.append(_step_criterion(agent, state, catalog))

    for rule in _requested_rules(user_request):
        try:
            ok, evidence = rule["check"](state, catalog)
        except Exception as exc:  # a broken checker must not break the run
            logger.debug("Deliverable check %s failed: %s", rule["id"], exc)
            ok, evidence = False, f"检查失败: {exc}"
        criteria.append({
            "id": f"deliverable:{rule['id']}",
            "label": f"交付物 · {rule['label']}",
            "kind": "deliverable",
            "agent": rule.get("agent", ""),
            "required": True,
            "weight": 1.0,
            "status": "met" if ok else "unmet",
            "evidence": evidence,
        })

    return criteria


def _score(criteria: list[dict]) -> float:
    total = sum(c.get("weight", 1.0) for c in criteria) or 1.0
    earned = sum(
        c.get("weight", 1.0) * {"met": 1.0, "partial": 0.5}.get(c.get("status", "unmet"), 0.0)
        for c in criteria
    )
    return round(earned / total, 3)


def _status_from(criteria: list[dict], score: float) -> str:
    if not criteria:
        return COMPLETE
    if all(c.get("status") == "met" for c in criteria):
        return COMPLETE
    # Nothing at all was achieved, and at least one planned step failed.
    if (
        not any(c.get("status") == "met" for c in criteria)
        and any(c.get("kind") == "step" for c in criteria)
    ):
        return FAILED
    if score >= 0.99:
        return COMPLETE
    if score >= 0.5:
        return PARTIAL
    return INCOMPLETE


# ── LLM judge ──────────────────────────────────────────────────────────


def _artifacts_digest(state: dict) -> str:
    lines: list[str] = []
    charts = [u for u in (_entry_url(e) for e in (state.get("chart_results") or [])) if u]
    if charts:
        lines.append(f"- 图表 {len(charts)} 个: {', '.join(charts[:3])}")
    images = [u for u in (_entry_url(e) for e in (state.get("image_results") or [])) if u]
    if images:
        lines.append(f"- 图片 {len(images)} 张: {', '.join(images[:3])}")
    videos = [u for u in (_entry_url(e) for e in (state.get("video_results") or [])) if u]
    if videos:
        lines.append(f"- 视频 {len(videos)} 个: {', '.join(videos[:3])}")
    codes = state.get("code_results") or []
    if codes:
        lines.append(f"- 代码结果 {len(codes)} 条")
    research = state.get("research_results") or []
    if research:
        sources = sum(
            len(r.get("sources") or []) for r in research if isinstance(r, dict)
        )
        lines.append(f"- 检索结果 {len(research)} 条 / 来源 {sources} 个")
    for item in _html_artifacts(state)[:5]:
        lines.append(f"- HTML 交付物 [{item['title'] or item['skill']}]({item['url']})")
    return "\n".join(lines) if lines else "（无任何产出物）"


def _parse_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def llm_verify(
    *,
    user_request: str,
    criteria: list[dict],
    state: dict,
    model_id: str = "",
    final_response: str = "",
) -> tuple[Optional[dict], str]:
    """Run the LLM judge. Returns ``(verdict, error)``."""
    from backend.models.provider import create_chat_model

    plan_summary = "\n".join(
        f"- [{c.get('status')}] {c.get('label')} — {c.get('evidence')}"
        for c in criteria
    ) or "（无步骤）"

    prompt = GOAL_VERIFICATION_PROMPT.format(
        user_request=(user_request or "")[:800],
        plan_summary=plan_summary,
        artifacts=_artifacts_digest(state),
        final_response=(final_response or "（尚未生成最终回复）")[:1500],
    )

    try:
        llm = create_chat_model(
            model_id=model_id or "deepseek-ai/DeepSeek-V3",
            temperature=0.0,
            max_tokens=700,
        )
        response = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=prompt)]), timeout=LLM_VERIFY_TIMEOUT
        )
    except asyncio.TimeoutError:
        return None, "目标完成确认调用超时"
    except Exception as exc:  # provider errors must not break the run
        return None, f"目标完成确认调用失败: {exc}"

    content = response.content if hasattr(response, "content") else str(response)
    data = _parse_json(content)
    if not data:
        return None, "目标完成确认返回了无法解析的内容"
    return data, ""


# ── public API ─────────────────────────────────────────────────────────


def summarize_verdict(
    user_request: str,
    tasks: list[dict],
    state: dict,
    catalog: Optional[dict[str, dict]] = None,
) -> dict:
    """Deterministic-only verification (no model call)."""
    criteria = deterministic_checks(user_request, tasks, state, catalog)
    score = _score(criteria)
    status = _status_from(criteria, score)
    return {
        "status": status,
        "score": score,
        "criteria": criteria,
        "unmet": [c for c in criteria if c.get("status") != "met"],
        "checked_by": ["deterministic"],
        "llm": None,
        "llm_error": "",
        "summary": _summary_text(status, criteria),
    }


async def verify_goal_completion(
    *,
    user_request: str,
    tasks: list[dict],
    state: dict,
    catalog: Optional[dict[str, dict]] = None,
    model_id: str = "",
    final_response: str = "",
    use_llm: bool = True,
) -> dict:
    """Full confirmation: deterministic evidence plus an optional LLM judge."""
    verdict = summarize_verdict(user_request, tasks, state, catalog)

    if use_llm:
        data, error = await llm_verify(
            user_request=user_request,
            criteria=verdict["criteria"],
            state=state,
            model_id=model_id,
            final_response=final_response,
        )
        verdict["checked_by"].append("llm")
        verdict["llm_error"] = error
        if data:
            verdict["llm"] = {
                "complete": bool(data.get("complete")),
                "confidence": _clamp(data.get("confidence")),
                "missing": _str_list(data.get("missing")),
                "unverified_claims": _str_list(data.get("unverified_claims")),
                "reason": str(data.get("reason") or "")[:500],
            }
            _reconcile(verdict)

    verdict["summary"] = _summary_text(verdict["status"], verdict["criteria"], verdict.get("llm"))
    return verdict


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(number, 2)))


def _str_list(value: Any, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        return [value[:300]]
    if isinstance(value, list):
        return [str(v)[:300] for v in value[:limit]]
    return []


def _reconcile(verdict: dict) -> None:
    """Let the LLM judge tighten — never loosen — the deterministic verdict."""
    llm = verdict["llm"]
    if not llm:
        return
    if llm["complete"] and verdict["status"] in (INCOMPLETE, FAILED):
        # model says done but evidence says otherwise → keep evidence, note doubt
        llm["conflicts_with_evidence"] = True
        return
    if not llm["complete"] and llm["confidence"] >= 0.6 and verdict["status"] == COMPLETE:
        verdict["status"] = PARTIAL
        llm["downgraded"] = True


def _summary_text(status: str, criteria: list[dict], llm: Optional[dict] = None) -> str:
    unmet = [c["label"] for c in criteria if c.get("status") != "met"]
    label = {
        COMPLETE: "目标已完成",
        PARTIAL: "目标部分完成",
        INCOMPLETE: "目标未完成",
        FAILED: "目标执行失败",
    }.get(status, status)
    if unmet:
        label += "：" + "、".join(unmet[:4])
        if len(unmet) > 4:
            label += f" 等 {len(unmet)} 项"
    if llm and llm.get("reason"):
        label += f"（模型确认：{llm['reason']}）"
    return label


# ── remediation ────────────────────────────────────────────────────────


def build_remediation_tasks(
    verdict: dict,
    tasks: list[dict],
    catalog: Optional[dict[str, dict]] = None,
    *,
    max_tasks: int = 3,
) -> list[dict]:
    """Turn unmet criteria into concrete follow-up tasks.

    Only safe (low/medium risk) producers are eligible, and the caller is
    responsible for the budget check — this function never bypasses it.
    """
    existing_agents = {t.get("agent", "") for t in tasks}
    planned: list[str] = []
    additions: list[dict] = []

    def _safe(agent: str) -> bool:
        entry = (catalog or {}).get(agent) or {}
        risk = entry.get("risk", "medium")
        # Only agents this deployment can actually run, and only low/medium risk.
        return bool(entry.get("has_worker", False)) and risk in ("low", "medium")

    for criterion in verdict.get("unmet", []):
        if len(additions) >= max_tasks:
            break
        agent = criterion.get("agent") or ""
        if not agent or agent in planned or not _safe(agent):
            continue

        if criterion.get("id", "").startswith("deliverable:"):
            chain = _remediation_chain(criterion)
        else:
            chain = [agent]

        for step_agent in chain:
            if len(additions) >= max_tasks:
                break
            if step_agent in planned:
                continue
            additions.append({
                "agent": step_agent,
                "prompt": (
                    f"补齐未完成的产出（{criterion.get('label', '')}）："
                    f"{criterion.get('evidence', '')}。"
                    f"请基于已有结果重新生成，确保产出真实存在。"
                ),
                "_remediation": True,
                "_criterion": criterion.get("id", ""),
            })
            planned.append(step_agent)

    # A freshly injected dependency must not be scheduled after its consumer.
    for addition in additions:
        entry = (catalog or {}).get(addition["agent"]) or {}
        deps = [d for d in (entry.get("depends_on") or []) if d in existing_agents]
        if deps:
            addition["_after"] = deps

    return additions


_DELIVERABLE_CHAINS: dict[str, list[str]] = {
    "deliverable:chart": ["analyst", "chart"],
    "deliverable:data_table": ["analyst"],
    "deliverable:html_deck": ["ppt_planner", "ppt_slide", "ppt_assembler"],
}


def _remediation_chain(criterion: dict) -> list[str]:
    chain = _DELIVERABLE_CHAINS.get(criterion.get("id", ""))
    if chain:
        return list(chain)
    return [criterion.get("agent") or ""]


def artifact_snapshot(state: dict) -> dict:
    """Compact, JSON-safe summary of what a run actually produced."""
    return {
        "research": len(state.get("research_results") or []),
        "analyst": len(state.get("analyst_results") or []),
        "charts": len(state.get("chart_results") or []),
        "images": len(state.get("image_results") or []),
        "videos": len(state.get("video_results") or []),
        "code": len(state.get("code_results") or []),
        "html": len(_html_artifacts(state)),
    }
