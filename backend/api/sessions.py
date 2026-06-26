"""Session management routes."""
from fastapi import APIRouter, Request
from backend.sessions import (
    create_session, get_session, save_messages,
    list_sessions, delete_session,
)

router = APIRouter(tags=["sessions"])


@router.post("/sessions")
async def api_create_session():
    sid = await create_session()
    return {"session_id": sid}


@router.get("/sessions")
async def api_list_sessions():
    sessions = await list_sessions()
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def api_get_session(session_id: str):
    session = await get_session(session_id)
    if session is None:
        return {"error": "Session not found"}
    return session


@router.put("/sessions/{session_id}")
async def api_save_session(session_id: str, request: Request):
    body = await request.json()
    msgs = body.get("messages", [])
    ok = await save_messages(session_id, msgs)
    if not ok:
        return {"error": "Session not found"}
    return {"saved": len(msgs)}


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str):
    ok = await delete_session(session_id)
    if not ok:
        return {"error": "Session not found"}
    return {"deleted": True}
