"""Human-in-the-loop approval store.

When the policy engine escalates an action to ``review``, execution must stop
until a human decides.  The streaming orchestrator emits an ``approval_required``
SSE event, then awaits the matching request here while the browser POSTs the
decision to ``/api/safety/approvals/{id}``.

Fail-closed rules:

* Timeout → ``rejected`` (never silently approved).
* Process exit with a pending request → stays pending, and a later run cannot
  accidentally reuse the id.
* ``auto_approve`` is an explicit operator setting, never automatic on failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from backend.safety.settings import get_safety_settings

logger = logging.getLogger("safety.approvals")

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
TIMEOUT = "timeout"


@dataclass
class ApprovalRequest:
    """One escalation awaiting a human decision."""

    id: str
    run_id: str
    stage: str
    risk: str
    summary: str
    reasons: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    agent: str = ""
    task: str = ""
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = PENDING
    decided_at: Optional[float] = None
    decided_by: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["age_seconds"] = round(time.time() - self.created_at, 1)
        return data


class ApprovalStore:
    """In-memory registry of pending approvals with async waiters."""

    def __init__(self, history_limit: int = 200):
        self._requests: dict[str, ApprovalRequest] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._history: list[ApprovalRequest] = []
        self._history_limit = history_limit
        self._lock = asyncio.Lock()
        # Lifetime counters — decided requests leave ``_requests``, so the
        # statistics cannot be derived from the live tables alone.
        self._counters = {"approved": 0, "rejected": 0, "timeout": 0}

    # ── creation ───────────────────────────────────────────────────────

    async def create(
        self,
        *,
        run_id: str,
        stage: str,
        risk: str,
        summary: str,
        reasons: Optional[list[str]] = None,
        findings: Optional[list[dict]] = None,
        agent: str = "",
        task: str = "",
        session_id: str = "",
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            stage=stage,
            risk=risk,
            summary=summary,
            reasons=list(reasons or []),
            findings=list(findings or []),
            agent=agent,
            task=task[:600],
            session_id=session_id,
        )
        async with self._lock:
            self._requests[request.id] = request
            self._events[request.id] = asyncio.Event()
        logger.info(
            "Approval requested %s (run=%s stage=%s risk=%s)",
            request.id, run_id, stage, risk,
        )
        return request

    # ── decisions ──────────────────────────────────────────────────────

    async def decide(
        self,
        request_id: str,
        approved: bool,
        *,
        decided_by: str = "user",
        note: str = "",
    ) -> Optional[ApprovalRequest]:
        """Resolve a pending request. Returns None when the id is unknown."""
        async with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                return None
            if request.status != PENDING:
                return request
            request.decided_at = time.time()
            request.decided_by = decided_by
            request.note = note[:400]
            if approved:
                request.status = APPROVED
                self._counters["approved"] += 1
            else:
                # A timeout is a rejection, but recorded distinctly so
                # operators can tell "nobody answered" from "human said no".
                request.status = TIMEOUT if decided_by == "timeout" else REJECTED
                self._counters["rejected"] += 1
                if request.status == TIMEOUT:
                    self._counters["timeout"] += 1
            event = self._events.get(request_id)
            self._archive(request)
        if event is not None:
            event.set()
        logger.info("Approval %s → %s by %s", request_id, request.status, decided_by)
        return request

    async def wait(self, request_id: str, timeout: float = 180.0) -> ApprovalRequest:
        """Block until the request is decided, or fail closed on timeout.

        The returned object is the same instance ``decide`` mutated, so its
        ``status`` is authoritative even after it left the pending table.
        """
        event = self._events.get(request_id)
        request = self._requests.get(request_id)
        if request is None:
            archived = self.find(request_id)
            if archived is None:
                raise KeyError(f"unknown approval request: {request_id}")
            return archived
        if request.status == PENDING and event is not None:
            try:
                await asyncio.wait_for(event.wait(), timeout=max(1.0, timeout))
            except asyncio.TimeoutError:
                await self.decide(
                    request_id, False, decided_by="timeout",
                    note=f"审批超时（{timeout:.0f}s）未响应，按拒绝处理",
                )
        return request

    async def decide_and_wait(
        self, request: ApprovalRequest, approved: bool, *, decided_by: str = "policy"
    ) -> ApprovalRequest:
        await self.decide(request.id, approved, decided_by=decided_by)
        return request

    # ── queries ────────────────────────────────────────────────────────

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        return self._requests.get(request_id)

    def find(self, request_id: str) -> Optional[ApprovalRequest]:
        """Look up a request whether it is still pending or already decided."""
        pending = self._requests.get(request_id)
        if pending is not None:
            return pending
        for item in reversed(self._history):
            if item.id == request_id:
                return item
        return None

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._requests.values() if r.status == PENDING]

    def history(self, limit: int = 50) -> list[ApprovalRequest]:
        return list(reversed(self._history[-max(1, limit):]))

    def stats(self) -> dict:
        return {
            "pending": len(self.pending()),
            "decided": self._counters["approved"] + self._counters["rejected"],
            "approved": self._counters["approved"],
            "rejected": self._counters["rejected"],
            "timeouts": self._counters["timeout"],
            "auto_approve": get_safety_settings().auto_approve,
        }

    # ── internals ──────────────────────────────────────────────────────

    def _archive(self, request: ApprovalRequest) -> None:
        self._history.append(request)
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]
        # Drop resolved waiters so the dict cannot grow without bound.
        self._requests.pop(request.id, None)
        self._events.pop(request.id, None)


_store: Optional[ApprovalStore] = None


def get_approval_store() -> ApprovalStore:
    global _store
    if _store is None:
        _store = ApprovalStore()
    return _store
