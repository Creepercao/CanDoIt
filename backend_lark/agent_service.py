"""Feishu/Lark-facing agent service built on top of the core Agent Loop."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter

from backend.agents.orchestrator import make_initial_state, run_agent_loop
from backend_lark.schemas import (
    AgentBaseRequest,
    AutomationAgentRequest,
    DataReportAgentRequest,
    DocumentAgentRequest,
    FeishuDispatchRequest,
    MeetingAgentRequest,
    PPTAgentRequest,
    PublishAction,
    ResearchAgentRequest,
)

router = APIRouter(tags=["feishu-agents"])


def public_url(path_or_url: str) -> str:
    if not path_or_url:
        return ""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8001").rstrip("/")
    if path_or_url.startswith("/"):
        return f"{base}{path_or_url}"
    return f"{base}/{path_or_url}"


def html_results(skill_outputs: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for skill_name, items in (skill_outputs or {}).items():
        for item in items or []:
            if isinstance(item, dict) and item.get("html_url"):
                html_url = item.get("html_url", "")
                results.append(
                    {
                        "skill_name": skill_name,
                        "html_url": html_url,
                        "public_html_url": public_url(html_url),
                        "html_title": item.get("html_title", ""),
                        "html_path": item.get("html_path", ""),
                        "deck_id": item.get("deck_id", ""),
                        "slides": item.get("slides"),
                        "failed_slides": item.get("failed_slides", []),
                    }
                )
    return results


def chart_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for item in state.get("chart_results", []) or []:
        if isinstance(item, dict):
            item = dict(item)
            if item.get("url"):
                item["public_url"] = public_url(item["url"])
            charts.append(item)
    return charts


async def run_core_agent(message: str, req: AgentBaseRequest) -> dict[str, Any]:
    state = make_initial_state(
        user_request=message,
        chat_model_id=req.chat_model_id,
        router_model_id=req.router_model_id,
        history=[],
    )
    result = await run_agent_loop(state)
    skill_outputs = result.get("skill_outputs", {}) or {}
    return {
        "response": result.get("final_response", ""),
        "tasks": result.get("tasks", []),
        "research_results": result.get("research_results", []),
        "analyst_results": result.get("analyst_results", []),
        "chart_results": chart_results(result),
        "html_results": html_results(skill_outputs),
        "skill_outputs": skill_outputs,
    }


def build_publish_actions(agent_id: str, req: AgentBaseRequest, payload: dict[str, Any]) -> list[dict[str, Any]]:
    chat_id = req.source.chat_id
    message_id = req.source.message_id
    actions: list[PublishAction] = []

    text = payload.get("response", "")
    if text:
        actions.append(
            PublishAction(
                type="send_markdown",
                title=f"{agent_id} result",
                text=text,
                chat_id=chat_id,
                message_id=message_id,
            )
        )

    for item in payload.get("html_results", []) or []:
        url = item.get("public_html_url") or public_url(item.get("html_url", ""))
        if url:
            actions.append(
                PublishAction(
                    type="send_link",
                    title=item.get("html_title") or "Generated presentation",
                    text="Open the generated local HTML deck.",
                    url=url,
                    chat_id=chat_id,
                    message_id=message_id,
                    metadata={
                        "deck_id": item.get("deck_id", ""),
                        "failed_slides": item.get("failed_slides", []),
                        "status_endpoint": f"/api/ppt-runs/{item.get('deck_id')}" if item.get("deck_id") else "",
                        "pptx_export_endpoint": "/api/skills/ppt-animation/export-pptx",
                    },
                )
            )

    for item in payload.get("chart_results", []) or []:
        url = item.get("public_url") or public_url(item.get("url", ""))
        if url:
            actions.append(
                PublishAction(
                    type="send_link",
                    title=item.get("title") or "Generated chart",
                    text="Open the generated chart.",
                    url=url,
                    chat_id=chat_id,
                    message_id=message_id,
                )
            )

    if not actions:
        actions.append(PublishAction(type="noop", text="No publishable result generated."))
    return [action.model_dump() for action in actions]


def common_response(agent_id: str, req: AgentBaseRequest, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "agent_id": agent_id,
        "source": req.source.model_dump(),
        "metadata": req.metadata,
        **payload,
        "publish_actions": build_publish_actions(agent_id, req, payload),
    }


@router.get("/agents")
async def list_agents() -> dict[str, Any]:
    return {
        "mode": "feishu-dedicated",
        "agents": [
            {"id": "research", "endpoint": "/api/agents/research"},
            {"id": "document", "endpoint": "/api/agents/document"},
            {"id": "ppt", "endpoint": "/api/agents/ppt"},
            {"id": "meeting", "endpoint": "/api/agents/meeting"},
            {"id": "data-report", "endpoint": "/api/agents/data-report"},
            {"id": "automation", "endpoint": "/api/agents/automation"},
        ],
        "publish_actions": ["send_markdown", "send_link", "upload_file", "noop"],
    }


@router.post("/agents/research")
async def run_research_agent(req: ResearchAgentRequest) -> dict[str, Any]:
    detail = "brief" if req.output_format == "brief" else "structured report"
    context = f"\n\nFeishu context:\n{req.context_text}" if req.context_text else ""
    message = (
        "You are a Feishu research agent. Research, verify, and summarize."
        f"\nOutput format: {detail}"
        f"\nRequest: {req.query or req.source.title}"
        f"{context}"
        "\nReturn Chinese Markdown suitable for a Feishu message or document."
    )
    payload = await run_core_agent(message, req)
    return common_response("research", req, payload)


@router.post("/agents/document")
async def run_document_agent(req: DocumentAgentRequest) -> dict[str, Any]:
    message = (
        "You are a Feishu document and knowledge agent. Work only from the provided context unless research is needed."
        f"\nMode: {req.mode}"
        f"\nTitle: {req.document_title or req.source.title or req.query}"
        f"\nRequest: {req.query}"
        f"\nDocument/context:\n{req.context_text}"
        "\nReturn Chinese Markdown that can be written back by a Feishu adapter."
    )
    payload = await run_core_agent(message, req)
    return common_response("document", req, payload)


@router.post("/agents/ppt")
async def run_ppt_agent(req: PPTAgentRequest) -> dict[str, Any]:
    context = f"\n\nFeishu context:\n{req.context_text}" if req.context_text else ""
    message = (
        "You are a Feishu presentation agent. Generate a report-ready HTML deck."
        f"\nTopic: {req.query or req.source.title}"
        f"\nSlide count: {req.slide_count}"
        f"\nTheme: {req.theme}"
        f"{context}"
        "\nUse the research -> ppt_planner -> ppt_slide -> ppt_assembler pipeline."
        "\nReturn the generated local HTML deck link and deck_id."
    )
    payload = await run_core_agent(message, req)
    return common_response("ppt", req, payload)


@router.post("/agents/meeting")
async def run_meeting_agent(req: MeetingAgentRequest) -> dict[str, Any]:
    transcript = req.transcript or req.context_text
    participants = ", ".join(req.participants) if req.participants else "not provided"
    message = (
        "You are a Feishu meeting agent."
        f"\nMeeting title: {req.meeting_title or req.source.title or req.query}"
        f"\nParticipants: {participants}"
        f"\nAction items required: {'yes' if req.create_action_items else 'no'}"
        f"\nTranscript/content:\n{transcript}"
        "\nReturn Chinese Markdown with summary, decisions, action items, risks, and follow-up."
    )
    payload = await run_core_agent(message, req)
    return common_response("meeting", req, payload)


@router.post("/agents/data-report")
async def run_data_report_agent(req: DataReportAgentRequest) -> dict[str, Any]:
    table_json = req.table_json[:200]
    table_json_text = f"\nTable JSON sample:\n{table_json}" if table_json else ""
    table_text = req.table_text or req.context_text
    message = (
        "You are a Feishu data-report agent. Analyze the provided Sheet/Base data."
        f"\nData title: {req.table_title or req.source.title or req.query}"
        f"\nChart required: {'yes' if req.chart_required else 'no'}"
        f"\nTable text:\n{table_text}"
        f"{table_json_text}"
        "\nReturn Chinese conclusions, anomalies, trends, and actions. Generate charts when possible."
    )
    payload = await run_core_agent(message, req)
    return common_response("data-report", req, payload)


@router.post("/agents/automation")
async def run_automation_agent(req: AutomationAgentRequest) -> dict[str, Any]:
    constraints = "\n".join(f"- {item}" for item in req.constraints)
    message = (
        "You are a Feishu automation and task-planning agent."
        f"\nGoal: {req.goal or req.query}"
        f"\nAction format: {req.action_format}"
        f"\nConstraints:\n{constraints}"
        f"\nContext:\n{req.context_text}"
        "\nReturn Chinese Markdown plus a clear structured action checklist that a Feishu adapter can create as tasks."
    )
    payload = await run_core_agent(message, req)
    return common_response("automation", req, payload)


def choose_agent(query: str, requested: str = "auto") -> str:
    if requested and requested != "auto":
        return requested
    text = (query or "").lower()
    if any(key in text for key in ["/ppt", "ppt", "slide", "deck", "演示", "汇报"]):
        return "ppt"
    if any(key in text for key in ["/meeting", "minutes", "meeting", "会议", "纪要"]):
        return "meeting"
    if any(key in text for key in ["/report", "sheet", "base", "table", "数据", "表格", "报表"]):
        return "data-report"
    if any(key in text for key in ["/doc", "document", "文档", "知识库", "总结"]):
        return "document"
    if any(key in text for key in ["/todo", "task", "workflow", "待办", "任务", "流程"]):
        return "automation"
    return "research"


@router.post("/dispatch")
async def dispatch(req: FeishuDispatchRequest) -> dict[str, Any]:
    agent = choose_agent(req.query or req.context_text, req.agent)
    if agent == "ppt":
        return await run_ppt_agent(PPTAgentRequest(**req.model_dump(exclude={"agent", "content_type", "raw_event"})))
    if agent == "meeting":
        return await run_meeting_agent(MeetingAgentRequest(**req.model_dump(exclude={"agent", "content_type", "raw_event"})))
    if agent == "data-report":
        return await run_data_report_agent(DataReportAgentRequest(**req.model_dump(exclude={"agent", "content_type", "raw_event"})))
    if agent == "document":
        return await run_document_agent(DocumentAgentRequest(**req.model_dump(exclude={"agent", "content_type", "raw_event"})))
    if agent == "automation":
        return await run_automation_agent(AutomationAgentRequest(**req.model_dump(exclude={"agent", "content_type", "raw_event"})))
    return await run_research_agent(ResearchAgentRequest(**req.model_dump(exclude={"agent", "content_type", "raw_event"})))

