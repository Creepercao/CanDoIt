"""Cross-session long-term memory.

Two kinds of experience are worth carrying between sessions, and they have very
different trust requirements:

* **facts and preferences** (``fact`` / ``preference``) — "我在做 X 项目",
  "报告一律用中文".  These are cheap to state, cheap to correct, and being
  wrong is low-stakes, so they are written automatically and recalled at once.
* **conclusions** (``conclusion``) — reusable research findings.  Being wrong
  here silently poisons future answers, so they are staged as ``pending`` and
  only become recallable after an explicit confirmation
  (``POST /api/memory/{id}/confirm``).

Storage is a single JSON document under ``outputs/memory/`` plus embedding
vectors, with recall by cosine similarity.  A few hundred memories makes
brute-force search instant, and keeping the store readable matters more here
than indexing: a wrong memory must be easy to find and delete.

When no embedding provider is reachable, recall degrades to token overlap
rather than silently returning nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("memory")

FACT = "fact"
PREFERENCE = "preference"
CONCLUSION = "conclusion"
KINDS = (FACT, PREFERENCE, CONCLUSION)

ACTIVE = "active"
PENDING = "pending"
REJECTED = "rejected"

AUTO_KINDS = (FACT, PREFERENCE)
_MAX_TEXT = 600
_MAX_ITEMS = 500

_WORD = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


# ── configuration ──────────────────────────────────────────────────────


def memory_enabled() -> bool:
    raw = os.environ.get("AGENT_MEMORY")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, floor: int = 0) -> int:
    try:
        return max(floor, int(float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, floor: float = 0.0) -> float:
    try:
        return max(floor, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def recall_top_k() -> int:
    return _env_int("AGENT_MEMORY_TOP_K", 5, floor=0)


def recall_min_score() -> float:
    return _env_float("AGENT_MEMORY_MIN_SCORE", 0.35)


# ── records ────────────────────────────────────────────────────────────


@dataclass
class Memory:
    """One remembered item."""

    id: str
    kind: str
    text: str
    status: str = ACTIVE
    tags: list[str] = field(default_factory=list)
    session_id: str = ""
    run_id: str = ""
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    confirmed_at: float = 0.0
    vector: list[float] = field(default_factory=list, repr=False)

    def to_dict(self, *, include_vector: bool = False) -> dict:
        data = asdict(self)
        if not include_vector:
            data.pop("vector", None)
        return data


# ── store ──────────────────────────────────────────────────────────────


def memory_dir() -> Path:
    from backend.api import OUTPUTS_DIR

    return OUTPUTS_DIR / "memory"


class MemoryStore:
    """JSON-backed memory store with semantic recall."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (memory_dir() / "memories.json")
        self._items: list[Memory] = []
        self._loaded = False

    # ── persistence ────────────────────────────────────────────────────

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Memory store unreadable (%s); starting empty", exc)
            return
        for raw in (data.get("memories") if isinstance(data, dict) else data) or []:
            if not isinstance(raw, dict) or not raw.get("text"):
                continue
            try:
                self._items.append(Memory(
                    id=str(raw.get("id") or uuid.uuid4().hex[:12]),
                    kind=str(raw.get("kind") or FACT),
                    text=str(raw["text"])[:_MAX_TEXT],
                    status=str(raw.get("status") or ACTIVE),
                    tags=[str(t) for t in (raw.get("tags") or [])][:8],
                    session_id=str(raw.get("session_id") or ""),
                    run_id=str(raw.get("run_id") or ""),
                    confidence=float(raw.get("confidence") or 1.0),
                    created_at=float(raw.get("created_at") or time.time()),
                    confirmed_at=float(raw.get("confirmed_at") or 0.0),
                    vector=[float(v) for v in (raw.get("vector") or [])],
                ))
            except (TypeError, ValueError):
                continue
        logger.info(f"Memory store loaded: {len(self._items)} item(s)")

    def save(self) -> bool:
        payload = {
            "version": 1,
            "memories": [item.to_dict(include_vector=True) for item in self._items],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError as exc:
            logger.warning("Memory store persist failed: %s", exc)
            return False

    # ── writes ─────────────────────────────────────────────────────────

    async def add(
        self,
        text: str,
        *,
        kind: str = FACT,
        tags: Optional[list[str]] = None,
        session_id: str = "",
        run_id: str = "",
        confidence: float = 1.0,
        status: Optional[str] = None,
        vector: Optional[list[float]] = None,
    ) -> Optional[Memory]:
        """Add a memory; conclusions default to ``pending`` unless overridden."""
        self.load()
        text = (text or "").strip()[:_MAX_TEXT]
        if not text:
            return None
        # Never persist a memory that carries a credential, whatever the model
        # claimed — the store is long-lived and recalled into future prompts.
        from backend.safety.redact import scan_secrets

        if scan_secrets(text):
            logger.warning("Refused to store a memory containing a credential")
            return None
        kind = kind if kind in KINDS else FACT
        if status is None:
            status = PENDING if kind == CONCLUSION else ACTIVE
        if status not in (ACTIVE, PENDING, REJECTED):
            status = PENDING

        duplicate = self._find_duplicate(text)
        if duplicate is not None:
            # Refresh rather than pile up near-identical entries.
            duplicate.created_at = time.time()
            if confidence > duplicate.confidence:
                duplicate.confidence = confidence
            self.save()
            return duplicate

        item = Memory(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            text=text,
            status=status,
            tags=[str(t)[:40] for t in (tags or [])][:8],
            session_id=session_id,
            run_id=run_id,
            confidence=max(0.0, min(1.0, float(confidence))),
            vector=list(vector or []),
        )
        if vector is None:
            item.vector = await self._embed(text) or []
        self._items.append(item)
        self._trim()
        self.save()
        return item

    def _find_duplicate(self, text: str) -> Optional[Memory]:
        normalized = " ".join(text.split()).lower()
        for item in self._items:
            if " ".join(item.text.split()).lower() == normalized:
                return item
        return None

    def _trim(self) -> None:
        if len(self._items) <= _MAX_ITEMS:
            return
        # Drop the oldest rejected items first, then the oldest in general.
        self._items.sort(key=lambda m: (m.status != REJECTED, m.created_at))
        self._items = self._items[len(self._items) - _MAX_ITEMS:]

    def confirm(self, memory_id: str) -> Optional[Memory]:
        self.load()
        item = self.get(memory_id)
        if item is None:
            return None
        item.status = ACTIVE
        item.confirmed_at = time.time()
        self.save()
        return item

    def reject(self, memory_id: str) -> Optional[Memory]:
        self.load()
        item = self.get(memory_id)
        if item is None:
            return None
        item.status = REJECTED
        self.save()
        return item

    def delete(self, memory_id: str) -> bool:
        self.load()
        before = len(self._items)
        self._items = [item for item in self._items if item.id != memory_id]
        if len(self._items) == before:
            return False
        self.save()
        return True

    # ── reads ──────────────────────────────────────────────────────────

    def get(self, memory_id: str) -> Optional[Memory]:
        self.load()
        for item in self._items:
            if item.id == memory_id:
                return item
        return None

    def items(self, *, status: str = "", kind: str = "", limit: int = 200) -> list[Memory]:
        self.load()
        selected = [
            item for item in self._items
            if (not status or item.status == status) and (not kind or item.kind == kind)
        ]
        selected.sort(key=lambda m: m.created_at, reverse=True)
        return selected[: max(1, limit)]

    def stats(self) -> dict:
        self.load()
        counts: dict[str, int] = {}
        kinds: dict[str, int] = {}
        for item in self._items:
            counts[item.status] = counts.get(item.status, 0) + 1
            kinds[item.kind] = kinds.get(item.kind, 0) + 1
        return {
            "total": len(self._items),
            "by_status": counts,
            "by_kind": kinds,
            "pending": counts.get(PENDING, 0),
            "path": str(self.path),
        }

    async def recall(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        kinds: Optional[tuple[str, ...]] = None,
    ) -> list[dict]:
        """Return the memories most relevant to *query* (active ones only)."""
        self.load()
        query = (query or "").strip()
        if not query:
            return []
        top_k = recall_top_k() if top_k is None else top_k
        if top_k <= 0:
            return []
        min_score = recall_min_score() if min_score is None else min_score

        candidates = [
            item for item in self._items
            if item.status == ACTIVE and (kinds is None or item.kind in kinds)
        ]
        if not candidates:
            return []

        query_vector = await self._embed(query)
        scored: list[tuple[float, Memory]] = []
        for item in candidates:
            score = _similarity(query_vector, item.vector, query, item.text)
            if score >= min_score:
                scored.append((score, item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for score, item in scored[:top_k]:
            data = item.to_dict()
            data["score"] = round(score, 4)
            results.append(data)
        return results

    async def _embed(self, text: str) -> Optional[list[float]]:
        try:
            from backend.embedding import get_embedding

            return await get_embedding(text[:2000])
        except Exception as exc:
            logger.debug("Embedding unavailable for memory: %s", exc)
            return None

    # ── recall prompt ──────────────────────────────────────────────────

    async def recall_for_prompt(self, query: str) -> tuple[str, list[dict]]:
        """Render recalled memories for injection into a prompt."""
        if not memory_enabled():
            return "", []
        items = await self.recall(query)
        if not items:
            return "", []
        lines = [
            f"- [{item['kind']}] {item['text']}"
            for item in items
        ]
        block = (
            "以下是跨会话记忆（用户偏好与既有事实），请在满足当前请求时遵守；"
            "如果与当前请求冲突，以当前请求为准：\n" + "\n".join(lines)
        )
        return block, items


# ── similarity ─────────────────────────────────────────────────────────


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _WORD.findall(text or "")}


def token_overlap(a: str, b: str) -> float:
    """Jaccard-ish overlap used when embeddings are unavailable."""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _similarity(query_vector, item_vector, query_text: str, item_text: str) -> float:
    if query_vector and item_vector and len(query_vector) == len(item_vector):
        from backend.embedding import cosine_similarity

        return cosine_similarity(query_vector, item_vector)
    return token_overlap(query_text, item_text)


# ── extraction ─────────────────────────────────────────────────────────


def _parse_items(text: str) -> list[dict]:
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


async def extract_memories(
    *,
    user_request: str,
    final_response: str = "",
    state: Optional[dict] = None,
    model_id: str = "",
    timeout: float = 40.0,
) -> list[dict]:
    """Ask the model what is worth remembering from this run.

    Returns ``[{kind, text, confidence}]``; never raises.
    """
    if not memory_enabled():
        return []

    from backend.prompts import MEMORY_EXTRACTION_PROMPT

    state = state or {}
    findings = []
    for entry in (state.get("research_results") or [])[:2]:
        if isinstance(entry, dict) and entry.get("synthesis"):
            findings.append(str(entry["synthesis"])[:1200])

    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_request=(user_request or "")[:800],
        response=(final_response or "")[:1600],
        findings="\n---\n".join(findings) or "（本次没有研究结果）",
    )

    import asyncio

    try:
        from backend.models.provider import create_chat_model
        from langchain_core.messages import HumanMessage

        llm = create_chat_model(
            model_id=model_id or "deepseek-ai/DeepSeek-V3", temperature=0.0, max_tokens=600
        )
        response = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=prompt)]), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.debug("Memory extraction timed out")
        return []
    except Exception as exc:
        logger.debug("Memory extraction unavailable: %s", exc)
        return []

    content = response.content if hasattr(response, "content") else str(response)
    extracted: list[dict] = []
    for item in _parse_items(content)[:6]:
        text = str(item.get("text") or "").strip()
        kind = str(item.get("kind") or FACT).strip().lower()
        if not text or kind not in KINDS:
            continue
        if len(text) < 4:
            continue
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        extracted.append({
            "kind": kind,
            "text": text[:_MAX_TEXT],
            "confidence": max(0.0, min(1.0, confidence)),
        })
    return extracted


async def remember_run(
    *,
    user_request: str,
    final_response: str = "",
    state: Optional[dict] = None,
    model_id: str = "",
) -> dict:
    """Extract and store memories from a finished run.

    Facts/preferences go in immediately; conclusions are staged as ``pending``
    so a human confirms them before they can influence later answers.
    """
    result = {"stored": [], "pending": [], "skipped": ""}
    if not memory_enabled():
        result["skipped"] = "记忆功能已关闭（AGENT_MEMORY=0）"
        return result

    candidates = await extract_memories(
        user_request=user_request, final_response=final_response,
        state=state, model_id=model_id,
    )
    if not candidates:
        result["skipped"] = "本次没有可沉淀的记忆"
        return result

    store = get_memory_store()
    state = state or {}
    for candidate in candidates:
        item = await store.add(
            candidate["text"],
            kind=candidate["kind"],
            confidence=candidate["confidence"],
            session_id=str(state.get("session_id") or ""),
            run_id=str(state.get("run_id") or ""),
            tags=[candidate["kind"]],
        )
        if item is None:
            continue
        entry = {"id": item.id, "kind": item.kind, "status": item.status,
                 "text": item.text}
        if item.status == PENDING:
            result["pending"].append(entry)
        else:
            result["stored"].append(entry)

    try:
        from backend.safety.audit import get_audit_log

        get_audit_log().record(
            "memory.extracted",
            run_id=str(state.get("run_id") or ""),
            session_id=str(state.get("session_id") or ""),
            stage="memory",
            actor="system",
            decision="allow",
            risk="low",
            severity="info",
            message=f"沉淀 {len(result['stored'])} 条记忆、{len(result['pending'])} 条待确认",
            details={"stored": result["stored"], "pending": result["pending"]},
        )
    except Exception as exc:
        logger.debug("Memory audit skipped: %s", exc)

    return result


# ── singleton ──────────────────────────────────────────────────────────


_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
