"""Feishu/Lark event payload adapter.

This module accepts Feishu/Lark-shaped callback payloads and converts them to
the dedicated local agent dispatch contract. It does not call Feishu/Lark Open
API by itself; returned `publish_actions` are for an outer gateway or bot sender.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend_lark.agent_service import dispatch
from backend_lark.schemas import FeishuDispatchRequest, FeishuSource

router = APIRouter(tags=["feishu-events"])


def _get_nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _find_challenge(data: dict[str, Any]) -> str:
    value = data.get("challenge")
    if isinstance(value, str):
        return value
    value = _get_nested(data, "event", "challenge")
    if isinstance(value, str):
        return value
    return ""


def _event_token(data: dict[str, Any]) -> str:
    for path in [
        ("token",),
        ("header", "token"),
        ("event", "token"),
    ]:
        value = _get_nested(data, *path)
        if isinstance(value, str):
            return value
    return ""


def _verify_token(data: dict[str, Any]) -> None:
    expected = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
    if expected and _event_token(data) != expected:
        raise HTTPException(status_code=403, detail="Invalid Feishu verification token")


def _parse_message_content(content: Any) -> str:
    if isinstance(content, dict):
        value = content.get("text") or content.get("content") or ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except Exception:
        return content
    if isinstance(parsed, dict):
        value = parsed.get("text") or parsed.get("content") or ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return content


def extract_message(data: dict[str, Any]) -> tuple[str, FeishuSource]:
    event = data.get("event") if isinstance(data.get("event"), dict) else data
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}

    text = _parse_message_content(message.get("content") or event.get("content") or data.get("content"))

    source = FeishuSource(
        source_type="im",
        source_id=str(message.get("message_id") or event.get("message_id") or data.get("message_id") or ""),
        title=str(message.get("message_type") or event.get("event_type") or ""),
        chat_id=str(message.get("chat_id") or event.get("chat_id") or data.get("chat_id") or ""),
        message_id=str(message.get("message_id") or event.get("message_id") or data.get("message_id") or ""),
        sender_id=str(sender_id.get("user_id") or sender.get("sender_id") or ""),
        open_id=str(sender_id.get("open_id") or ""),
        tenant_key=str(_get_nested(data, "header", "tenant_key") or event.get("tenant_key") or ""),
    )
    return text.strip(), source


@router.post("/feishu/events")
async def handle_feishu_event(request: Request) -> dict[str, Any]:
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid event payload")

    _verify_token(data)
    challenge = _find_challenge(data)
    if challenge:
        return {"challenge": challenge}

    query, source = extract_message(data)
    if not query:
        return {
            "code": 0,
            "msg": "ignored",
            "reason": "No text content found in event payload.",
        }

    result = await dispatch(
        FeishuDispatchRequest(
            query=query,
            context_text="",
            source=source,
            raw_event=data,
        )
    )
    return {
        "code": 0,
        "msg": "ok",
        "result": result,
        "publish_actions": result.get("publish_actions", []),
    }


@router.post("/feishu/dispatch")
async def dispatch_feishu_request(req: FeishuDispatchRequest) -> dict[str, Any]:
    result = await dispatch(req)
    return {
        "code": 0,
        "msg": "ok",
        "result": result,
        "publish_actions": result.get("publish_actions", []),
    }

