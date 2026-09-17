"""Execution chain: a per-run span tree for skills and agents.

The audit log records *decisions* and the run record records *totals*; neither
answers "what actually happened, in what order, and where did the time go" for a
single run.  This adds that: every step of a run — planning, each task, each
retry attempt, each review, verification, remediation, synthesis — becomes a
span with a parent, a duration, a status and free-form attributes.

Spans nest automatically.  The current span lives in a ``ContextVar``, so a
parallel wave (``asyncio.gather``) parents each task correctly instead of
interleaving them on a shared stack::

    with chain.span("task", "research", agent="research") as task_span:
        with chain.span("attempt", "#1") as attempt:
            ...
            attempt.set(status="ok", output_chars=812)

The rendered form is deliberately plain text so a run can be pasted into an
issue or a log without any viewer.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

logger = logging.getLogger("observability.chain")

MAX_SPANS = 300

# Span kinds — kept as plain strings so an unknown kind never breaks rendering.
KIND_RUN = "run"
KIND_PLAN = "plan"
KIND_REVIEW = "review"
KIND_TASK = "task"
KIND_ATTEMPT = "attempt"
KIND_EXECUTE = "execute"
KIND_VERIFY = "verify"
KIND_REMEDIATE = "remediate"
KIND_MEMORY = "memory"
KIND_SYNTH = "synth"
KIND_BUDGET = "budget"

_OK = "ok"
_FAILED = "failed"
_BLOCKED = "blocked"
_RETRIED = "retried"
_SKIPPED = "skipped"

_STATUS_ICON = {
    _OK: "✓",
    _FAILED: "✗",
    _BLOCKED: "⛔",
    _RETRIED: "🔁",
    _SKIPPED: "·",
}


@dataclass
class Span:
    """One step of a run."""

    span_id: str
    kind: str
    name: str
    parent_id: str = ""
    agent: str = ""
    seq: int = 0
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: str = _OK
    error: str = ""
    attributes: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        if self.ended_at:
            return max(0.0, self.ended_at - self.started_at)
        return max(0.0, time.time() - self.started_at)

    # ── mutation while the span is open ────────────────────────────────

    def set(self, **attributes: Any) -> "Span":
        """Attach attributes.

        ``status`` and ``error`` are treated specially rather than landing in
        ``attributes`` — otherwise ``span.set(error="boom")`` would silently
        produce a span that claims success.
        """
        status = attributes.pop("status", None)
        if status:
            self.status = str(status)
        error = attributes.pop("error", None)
        if error:
            self.error = _compact(error)
        for key, value in attributes.items():
            if value is None:
                continue
            self.attributes[key] = _compact(value)
        return self

    def fail(self, error: Any, **attributes: Any) -> "Span":
        self.status = _FAILED
        self.error = _compact(error)
        return self.set(**attributes)

    def block(self, reason: Any = "", **attributes: Any) -> "Span":
        self.status = _BLOCKED
        if reason:
            self.error = _compact(reason)
        return self.set(**attributes)

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "agent": self.agent,
            "seq": self.seq,
            "started_at": round(self.started_at, 4),
            "duration": round(self.duration, 3),
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
        }


def _compact(value: Any, limit: int = 300) -> Any:
    """Trim a value for storage, keeping numbers/bools typed."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class RunChain:
    """Ordered span tree for one run."""

    def __init__(self, run_id: str = "", max_spans: int = MAX_SPANS):
        self.run_id = run_id
        self.max_spans = max_spans
        self._spans: list[Span] = []
        self._dropped = 0
        self._current: contextvars.ContextVar[str] = contextvars.ContextVar(
            f"span_current_{run_id or uuid.uuid4().hex[:8]}", default=""
        )

    @classmethod
    def disabled(cls) -> "RunChain":
        """A chain that records nothing — lets call sites stay unconditional."""
        return cls(max_spans=0)

    # ── long-lived spans ───────────────────────────────────────────────

    def start(self, kind: str, name: str, *, agent: str = "",
              parent_id: Optional[str] = None, **attributes: Any) -> Span:
        """Open a span that must outlive a ``with`` block (e.g. the whole run)."""
        span = self._new_span(kind, name, agent, parent_id)
        span.set(**attributes)
        self._append(span)
        return span

    def finish(self, span: Optional[Span], *, status: str = _OK,
               error: Any = "", **attributes: Any) -> Optional[Span]:
        """Close a span opened with :meth:`start`."""
        if span is None:
            return None
        if not span.ended_at:
            span.ended_at = time.time()
        if status:
            span.status = status
        if error:
            span.error = _compact(error)
        span.set(**attributes)
        return span

    # ── collection ─────────────────────────────────────────────────────

    def add(
        self,
        kind: str,
        name: str,
        *,
        agent: str = "",
        parent_id: Optional[str] = None,
        duration: float = 0.0,
        status: str = _OK,
        error: Any = "",
        **attributes: Any,
    ) -> Span:
        """Record a completed span (for events that need no timing block)."""
        span = self._new_span(kind, name, agent, parent_id)
        span.status = status
        span.error = _compact(error) if error else ""
        span.ended_at = span.started_at + max(0.0, duration)
        span.set(**attributes)
        self._append(span)
        return span

    @contextmanager
    def span(
        self,
        kind: str,
        name: str,
        *,
        agent: str = "",
        parent_id: Optional[str] = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """Open a timed span, nested under the innermost open span."""
        span = self._new_span(kind, name, agent, parent_id)
        span.set(**attributes)
        self._append(span)

        token = self._current.set(span.span_id)
        try:
            yield span
        except Exception as exc:                      # record, then re-raise
            span.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if not span.ended_at:
                span.ended_at = time.time()
            self._current.reset(token)

    def _new_span(self, kind: str, name: str, agent: str,
                  parent_id: Optional[str]) -> Span:
        if parent_id is None:
            parent_id = self._current.get()
        return Span(
            span_id=uuid.uuid4().hex[:10],
            kind=kind,
            name=name,
            parent_id=parent_id,
            agent=agent,
            seq=len(self._spans) + self._dropped,
        )

    def _append(self, span: Span) -> None:
        if len(self._spans) >= self.max_spans:
            self._dropped += 1
            return
        self._spans.append(span)

    # ── reads ──────────────────────────────────────────────────────────

    def spans(self) -> list[Span]:
        return list(self._spans)

    def children_of(self, span_id: str) -> list[Span]:
        return [s for s in self._spans if s.parent_id == span_id]

    def find(self, kind: str = "", agent: str = "") -> list[Span]:
        return [
            s for s in self._spans
            if (not kind or s.kind == kind) and (not agent or s.agent == agent)
        ]

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for span in self._spans:
            kinds[span.kind] = kinds.get(span.kind, 0) + 1
            statuses[span.status] = statuses.get(span.status, 0) + 1
        return {
            "spans": len(self._spans),
            "dropped": self._dropped,
            "by_kind": kinds,
            "by_status": statuses,
            "failed": statuses.get(_FAILED, 0),
            "blocked": statuses.get(_BLOCKED, 0),
        }

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "summary": self.summary(),
            "spans": [s.to_dict() for s in self._spans],
        }

    # ── rendering ──────────────────────────────────────────────────────

    def render_text(self, max_width: int = 120) -> str:
        """Plain-text tree, ordered by sequence.

        Orphans (a parent dropped by the span cap) are rendered at top level so
        nothing silently disappears.
        """
        if not self._spans:
            return "(no spans recorded)"
        known = {s.span_id for s in self._spans}
        children: dict[str, list[Span]] = {}
        roots: list[Span] = []
        for span in self._spans:
            if span.parent_id and span.parent_id in known:
                children.setdefault(span.parent_id, []).append(span)
            else:
                roots.append(span)

        lines: list[str] = []
        truncated = self._dropped > 0

        def walk(span: Span, prefix: str, is_last: bool, depth: int) -> None:
            branch = "" if depth == 0 else ("└─ " if is_last else "├─ ")
            icon = _STATUS_ICON.get(span.status, "?")
            detail = " ".join(
                f"{key}={value}" for key, value in span.attributes.items()
            )
            line = (
                f"{prefix}{branch}{icon} {span.kind}:{span.name} "
                f"[{span.duration:.2f}s]"
                f"{' ' + detail if detail else ''}"
                f"{'  ⚠ ' + span.error if span.error else ''}"
            )
            lines.append(line[:max_width])

            kids = children.get(span.span_id, [])
            child_prefix = prefix + ("" if depth == 0 else ("   " if is_last else "│  "))
            for index, child in enumerate(kids):
                walk(child, child_prefix, index == len(kids) - 1, depth + 1)

        for index, root in enumerate(roots):
            walk(root, "", index == len(roots) - 1, 0)

        if truncated:
            lines.append(f"… {self._dropped} span(s) dropped (cap {self.max_spans})")
        return "\n".join(lines)
