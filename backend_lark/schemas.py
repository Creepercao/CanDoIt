"""Schemas for the Feishu/Lark dedicated backend."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FeishuSource(BaseModel):
    source_type: str = "unknown"
    source_id: str = ""
    title: str = ""
    url: str = ""
    chat_id: str = ""
    message_id: str = ""
    sender_id: str = ""
    open_id: str = ""
    tenant_key: str = ""


class AgentBaseRequest(BaseModel):
    source: FeishuSource = Field(default_factory=FeishuSource)
    query: str = ""
    context_text: str = ""
    chat_model_id: str = ""
    router_model_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchAgentRequest(AgentBaseRequest):
    output_format: Literal["brief", "report"] = "report"


class DocumentAgentRequest(AgentBaseRequest):
    document_title: str = ""
    mode: Literal["summary", "rewrite", "faq", "knowledge"] = "summary"


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


class AutomationAgentRequest(AgentBaseRequest):
    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    action_format: Literal["checklist", "tasks", "workflow"] = "tasks"


class FeishuDispatchRequest(AgentBaseRequest):
    agent: Literal[
        "auto",
        "research",
        "document",
        "ppt",
        "meeting",
        "data-report",
        "automation",
    ] = "auto"
    content_type: str = "text"
    raw_event: dict[str, Any] = Field(default_factory=dict)


class PublishAction(BaseModel):
    type: Literal["send_text", "send_markdown", "send_link", "upload_file", "noop"]
    title: str = ""
    text: str = ""
    url: str = ""
    file_url: str = ""
    filename: str = ""
    chat_id: str = ""
    message_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

