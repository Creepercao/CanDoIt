"""Local service endpoints shaped for Feishu/Lark agent gateways.

These endpoints intentionally do not call Feishu/Lark APIs. They accept
normalized context from an external bot/workflow/gateway layer and return
structured local AI results that the caller can publish back to Feishu/Lark.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.agents.orchestrator import make_initial_state, run_agent_loop

router = APIRouter(tags=["lark-agents"])


class LarkSource(BaseModel):
    """Opaque references from the external Feishu/Lark adapter."""

    source_type: str = "unknown"
    source_id: str = ""
    title: str = ""
    url: str = ""
    chat_id: str = ""
    message_id: str = ""
    sender_id: str = ""


class AgentBaseRequest(BaseModel):
    source: LarkSource = Field(default_factory=LarkSource)
    query: str = ""
    context_text: str = ""
    chat_model_id: str = ""
    router_model_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchAgentRequest(AgentBaseRequest):
    output_format: Literal["brief", "report"] = "report"


class PPTAgentRequest(AgentBaseRequest):
    slide_count: int = Field(default=6, ge=1, le=20)
    theme: str = "dark-tech"
    export_pptx: bool = False


class MeetingAgentRequest(AgentBaseRequest):
    meeting_title: str = ""
    participants: list[str] = Field(default_factory=list)
    transcript: str = ""
    create_action_items: bool = True


class DataReportAgentRequest(AgentBaseRequest):
    table_title: str = ""
    table_text: str = ""
    table_json: list[dict[str, Any]] = Field(default_factory=list)
    chart_required: bool = True


def _html_results(skill_outputs: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for skill_name, items in (skill_outputs or {}).items():
        for item in items or []:
            if isinstance(item, dict) and item.get("html_url"):
                results.append(
                    {
                        "skill_name": skill_name,
                        "html_url": item.get("html_url", ""),
                        "html_title": item.get("html_title", ""),
                        "html_path": item.get("html_path", ""),
                        "deck_id": item.get("deck_id", ""),
                        "slides": item.get("slides"),
                        "failed_slides": item.get("failed_slides", []),
                    }
                )
    return results


def _chart_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    for item in state.get("chart_results", []) or []:
        if isinstance(item, dict):
            charts.append(item)
    return charts


async def _run_local_agent(message: str, req: AgentBaseRequest) -> dict[str, Any]:
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
        "chart_results": _chart_results(result),
        "html_results": _html_results(skill_outputs),
        "skill_outputs": skill_outputs,
    }


def _common_response(agent_id: str, req: AgentBaseRequest, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "agent_id": agent_id,
        "source": req.source.model_dump(),
        "metadata": req.metadata,
        **payload,
    }


@router.get("/lark-agents")
async def list_lark_agents() -> dict[str, Any]:
    return {
        "mode": "local-service",
        "note": "These endpoints do not call Feishu/Lark directly. External adapters publish returned results.",
        "agents": [
            {
                "id": "research",
                "name": "Feishu Research Agent",
                "endpoint": "/api/lark-agents/research",
                "inputs": ["query", "context_text", "source"],
                "outputs": ["response", "research_results"],
            },
            {
                "id": "ppt",
                "name": "Feishu PPT Agent",
                "endpoint": "/api/lark-agents/ppt",
                "inputs": ["query", "context_text", "slide_count", "theme", "source"],
                "outputs": ["response", "html_results", "skill_outputs"],
            },
            {
                "id": "meeting",
                "name": "Feishu Meeting Agent",
                "endpoint": "/api/lark-agents/meeting",
                "inputs": ["meeting_title", "transcript", "participants", "source"],
                "outputs": ["response", "action-summary markdown"],
            },
            {
                "id": "data-report",
                "name": "Feishu Data Report Agent",
                "endpoint": "/api/lark-agents/data-report",
                "inputs": ["table_title", "table_text", "table_json", "source"],
                "outputs": ["response", "chart_results"],
            },
        ],
    }


@router.post("/lark-agents/research")
async def run_research_agent(req: ResearchAgentRequest) -> dict[str, Any]:
    source_hint = f"\n\nSource: {req.source.title or req.source.source_type} {req.source.url}".strip()
    context = f"\n\nExternal context:\n{req.context_text}" if req.context_text else ""
    detail = "brief" if req.output_format == "brief" else "structured report"
    message = (
        "Act as a local research agent for a Feishu/Lark adapter. "
        "Research, verify, and summarize the request."
        f"\nOutput format: {detail}"
        f"\nUser request: {req.query or req.source.title}"
        f"{source_hint}{context}"
        "\nReturn a Chinese result that an external adapter can publish back to Feishu/Lark. Keep key sources."
    )
    payload = await _run_local_agent(message, req)
    return _common_response("research", req, payload)


@router.post("/lark-agents/ppt")
async def run_ppt_agent(req: PPTAgentRequest) -> dict[str, Any]:
    context = f"\n\nExternal context:\n{req.context_text}" if req.context_text else ""
    export_note = (
        "The caller may export the returned local HTML deck through the PPTX export API."
        if req.export_pptx
        else ""
    )
    message = (
        "Act as a local PPT agent for a Feishu/Lark adapter. "
        "Generate a report-ready HTML presentation."
        f"\nTopic: {req.query or req.source.title}"
        f"\nSlide count: {req.slide_count}"
        f"\nVisual theme: {req.theme}"
        f"{context}"
        "\nUse the research -> ppt_planner -> ppt_slide -> ppt_assembler pipeline and return the local HTML deck link."
        f"\n{export_note}"
    )
    payload = await _run_local_agent(message, req)
    return _common_response("ppt", req, payload)


@router.post("/lark-agents/meeting")
async def run_meeting_agent(req: MeetingAgentRequest) -> dict[str, Any]:
    transcript = req.transcript or req.context_text
    participants = ", ".join(req.participants) if req.participants else "not provided"
    message = (
        "Act as a local meeting-minutes agent for a Feishu/Lark adapter."
        f"\nMeeting title: {req.meeting_title or req.source.title or req.query}"
        f"\nParticipants: {participants}"
        f"\nAction items required: {'yes' if req.create_action_items else 'no'}"
        f"\nTranscript/content:\n{transcript}"
        "\nReturn Chinese Markdown with summary, decisions, action items, suggested owners, risks, and follow-up."
    )
    payload = await _run_local_agent(message, req)
    return _common_response("meeting", req, payload)


@router.post("/lark-agents/data-report")
async def run_data_report_agent(req: DataReportAgentRequest) -> dict[str, Any]:
    table_json = req.table_json[:200]
    table_json_text = f"\nTable JSON sample:\n{table_json}" if table_json else ""
    table_text = req.table_text or req.context_text
    message = (
        "Act as a local data-report agent for a Feishu/Lark adapter. "
        "Analyze the provided table/base data only."
        f"\nData title: {req.table_title or req.source.title or req.query}"
        f"\nChart required: {'yes' if req.chart_required else 'no'}"
        f"\nTable text:\n{table_text}"
        f"{table_json_text}"
        "\nReturn Chinese output with conclusions, anomalies, trends, and suggested actions. "
        "Generate charts when the data is structured enough."
    )
    payload = await _run_local_agent(message, req)
    return _common_response("data-report", req, payload)
