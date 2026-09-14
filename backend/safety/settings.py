"""Runtime safety settings — env defaults plus live overrides.

The GUI can flip ``auto_approve`` (skip human-in-the-loop prompts), switch the
enforcement ``mode``, and toggle the LLM judge.  Overrides persist to
``outputs/safety/settings.json`` so a restart keeps operator intent, and are
never trusted for *secrets* — only for behaviour switches.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("safety.settings")

VALID_MODES = ("off", "monitor", "enforce")
VALID_NONSTREAM = ("allow", "deny", "review")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "enabled")


def _env_choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw if raw in allowed else default


class SafetySettings:
    """Mutable safety configuration shared by the orchestrator and the API."""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            from backend.api import OUTPUTS_DIR

            path = OUTPUTS_DIR / "safety" / "settings.json"
        self.path = Path(path)
        self._lock = threading.Lock()
        self.mode: str = _env_choice("SAFETY_MODE", "enforce", VALID_MODES)
        self.auto_approve: bool = _env_bool("SAFETY_AUTO_APPROVE", False)
        self.llm_review: bool = _env_bool("SAFETY_LLM_REVIEW", True)
        self.goal_verification: bool = _env_bool("SAFETY_GOAL_VERIFICATION", True)
        self.auto_remediate: bool = _env_bool("SAFETY_AUTO_REMEDIATE", True)
        self.nonstream_review: str = _env_choice(
            "SAFETY_NONSTREAM_REVIEW", "allow", VALID_NONSTREAM
        )
        self.approval_timeout: float = float(
            os.environ.get("SAFETY_APPROVAL_TIMEOUT_SECONDS", "180") or 180
        )
        self.redact_outputs: bool = _env_bool("SAFETY_REDACT_OUTPUTS", True)
        # Capability switch: may the programmer agent run what it generates?
        self.code_execution: bool = _env_bool("AGENT_CODE_EXEC", True)
        self._load_overrides()

    # ── persistence ────────────────────────────────────────────────────

    def _load_overrides(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Safety settings load skipped: %s", exc)
            return
        if not isinstance(data, dict):
            return
        if data.get("mode") in VALID_MODES:
            self.mode = data["mode"]
        if data.get("nonstream_review") in VALID_NONSTREAM:
            self.nonstream_review = data["nonstream_review"]
        for key in ("auto_approve", "llm_review", "goal_verification",
                    "auto_remediate", "redact_outputs", "code_execution"):
            if isinstance(data.get(key), bool):
                setattr(self, key, data[key])
        if isinstance(data.get("approval_timeout"), (int, float)):
            self.approval_timeout = float(data["approval_timeout"])

    def _persist(self) -> None:
        # Only mutable settings are written; derived fields such as
        # ``enabled``/``enforcing`` are computed on read.
        payload = {
            "mode": self.mode,
            "auto_approve": self.auto_approve,
            "llm_review": self.llm_review,
            "goal_verification": self.goal_verification,
            "auto_remediate": self.auto_remediate,
            "nonstream_review": self.nonstream_review,
            "approval_timeout": self.approval_timeout,
            "redact_outputs": self.redact_outputs,
            "code_execution": self.code_execution,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("Safety settings persist skipped: %s", exc)

    # ── mutation ───────────────────────────────────────────────────────

    def update(self, **changes: Any) -> dict:
        """Apply whitelisted changes and return the new snapshot."""
        with self._lock:
            for key, value in changes.items():
                if value is None or not hasattr(self, key) or key == "path":
                    continue
                if key in ("mode", "nonstream_review"):
                    if value in VALID_MODES or value in VALID_NONSTREAM:
                        setattr(self, key, value)
                elif key == "approval_timeout":
                    try:
                        self.approval_timeout = max(5.0, float(value))
                    except (TypeError, ValueError):
                        continue
                elif isinstance(value, bool):
                    setattr(self, key, value)
            self._persist()
            return self.snapshot()

    # ── reporting ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """False when safety is switched off entirely."""
        return self.mode != "off"

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "enforcing": self.enforcing,
            "auto_approve": self.auto_approve,
            "llm_review": self.llm_review,
            "goal_verification": self.goal_verification,
            "auto_remediate": self.auto_remediate,
            "nonstream_review": self.nonstream_review,
            "approval_timeout": self.approval_timeout,
            "redact_outputs": self.redact_outputs,
            "code_execution": self.code_execution,
        }


_settings: Optional[SafetySettings] = None


def get_safety_settings() -> SafetySettings:
    global _settings
    if _settings is None:
        _settings = SafetySettings()
    return _settings
