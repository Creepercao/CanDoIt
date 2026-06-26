"""Session storage layer — thin wrapper over the existing cache (Redis or memory).

Each session holds:
  - session:{id}:meta   → {"title": str, "created_at": str}
  - session:{id}:msgs   → [{"role": "...", "content": "...", ...}, ...]

An index key ``sessions:index`` tracks all active session IDs.
"""
from __future__ import annotations

import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from backend.cache import cache

logger = logging.getLogger("sessions")

INDEX_KEY = "sessions:index"
META_SUFFIX = ":meta"
MSGS_SUFFIX = ":msgs"
SESSION_TTL = 7 * 24 * 3600  # 7 days


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _meta_key(session_id: str) -> str:
    return f"session:{session_id}{META_SUFFIX}"


def _msgs_key(session_id: str) -> str:
    return f"session:{session_id}{MSGS_SUFFIX}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _add_to_index(session_id: str) -> None:
    ids: list[str] = await cache.get(INDEX_KEY) or []
    if session_id not in ids:
        ids.append(session_id)
        await cache.set(INDEX_KEY, ids, ttl=SESSION_TTL)


async def _remove_from_index(session_id: str) -> None:
    ids: list[str] = await cache.get(INDEX_KEY) or []
    if session_id in ids:
        ids.remove(session_id)
        await cache.set(INDEX_KEY, ids, ttl=SESSION_TTL)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

async def create_session(title: str = "") -> str:
    """Create a new session and return its id."""
    session_id = uuid.uuid4().hex[:12]
    meta = {
        "title": title or "New Chat",
        "created_at": _now_iso(),
    }
    await cache.set(_meta_key(session_id), meta, ttl=SESSION_TTL)
    await cache.set(_msgs_key(session_id), [], ttl=SESSION_TTL)
    await _add_to_index(session_id)
    logger.info(f"Session created: {session_id}")
    return session_id


async def get_session(session_id: str) -> Optional[dict]:
    """Return session meta + messages, or None if not found."""
    meta = await cache.get(_meta_key(session_id))
    if meta is None:
        return None
    messages = await cache.get(_msgs_key(session_id)) or []
    return {"id": session_id, "meta": meta, "messages": messages}


async def save_messages(session_id: str, messages: list[dict]) -> bool:
    """Persist messages for a session. Returns False if session not found."""
    meta = await cache.get(_meta_key(session_id))
    if meta is None:
        return False
    await cache.set(_msgs_key(session_id), messages, ttl=SESSION_TTL)
    # Update title from first user message if still default
    if meta.get("title") in ("New Chat", ""):
        for m in messages:
            if m.get("role") == "user":
                meta["title"] = m.get("content", "")[:40]
                await cache.set(_meta_key(session_id), meta, ttl=SESSION_TTL)
                break
    return True


async def list_sessions() -> list[dict]:
    """Return all sessions with metadata (newest first)."""
    ids: list[str] = await cache.get(INDEX_KEY) or []
    result: list[dict] = []
    for sid in reversed(ids):  # newest first
        meta = await cache.get(_meta_key(sid))
        if meta:
            result.append({"id": sid, "meta": meta})
    return result


async def delete_session(session_id: str) -> bool:
    """Delete a session and its messages. Returns False if not found."""
    meta = await cache.get(_meta_key(session_id))
    if meta is None:
        return False
    await cache.delete(_meta_key(session_id))
    await cache.delete(_msgs_key(session_id))
    await _remove_from_index(session_id)
    logger.info(f"Session deleted: {session_id}")
    return True
