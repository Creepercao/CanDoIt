"""Agent safety layer — execution review, approvals, and goal confirmation.

Public surface
--------------

``SafetyReviewer``       per-run guard used by the orchestrator
``get_audit_log()``      hash-chained audit trail
``get_approval_store()`` human-in-the-loop approval registry
``get_safety_settings()`` runtime switches (``SAFETY_MODE``, auto-approve…)
``RunBudget``            per-run step/wave/replan/time limits

Everything is additive: with ``SAFETY_MODE=off`` the layer records nothing and
changes no decision.
"""

from __future__ import annotations

import uuid

from backend.safety.approvals import (
    APPROVED,
    PENDING,
    REJECTED,
    TIMEOUT,
    ApprovalRequest,
    ApprovalStore,
    get_approval_store,
)
from backend.safety.audit import AuditLog, get_audit_log
from backend.safety.budget import RunBudget
from backend.safety.injection import (
    screen,
    screen_untrusted_text,
    wrap_untrusted,
)
from backend.safety.policy import PolicyDecision
from backend.safety.redact import redact, scan_secrets
from backend.safety.reviewer import SafetyReviewer, build_agent_catalog
from backend.safety.settings import SafetySettings, get_safety_settings

__all__ = [
    "APPROVED",
    "PENDING",
    "REJECTED",
    "TIMEOUT",
    "ApprovalRequest",
    "ApprovalStore",
    "AuditLog",
    "PolicyDecision",
    "RunBudget",
    "SafetyReviewer",
    "SafetySettings",
    "build_agent_catalog",
    "get_approval_store",
    "get_audit_log",
    "get_safety_settings",
    "new_run_id",
    "redact",
    "scan_secrets",
    "screen",
    "screen_untrusted_text",
    "wrap_untrusted",
]


def new_run_id() -> str:
    """Allocate a short, unique run identifier."""
    return f"run_{uuid.uuid4().hex[:12]}"
