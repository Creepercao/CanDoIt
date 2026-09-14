"""Run budget — bounded execution limits for one agent run.

Unbounded agent loops are a safety problem: a mis-planned task graph can spawn
waves forever, and goal-driven remediation can keep re-planning.  ``RunBudget``
is a per-run counter object that every loop iteration must consult.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


@dataclass
class RunBudget:
    """Durable counters plus hard limits for a single run."""

    max_steps: int = 32
    max_waves: int = 12
    max_replans: int = 1
    max_wall_seconds: float = 1800.0
    max_llm_reviews: int = 6
    max_retries: int = 12

    steps: int = 0
    waves: int = 0
    replans: int = 0
    llm_reviews: int = 0
    retries: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # ── construction ───────────────────────────────────────────────────

    @classmethod
    def from_env(cls, **overrides) -> "RunBudget":
        params = {
            "max_steps": _env_int("AGENT_MAX_STEPS", 32),
            "max_waves": _env_int("AGENT_MAX_WAVES", 12),
            "max_replans": _env_int("AGENT_MAX_REPLANS", 1),
            "max_wall_seconds": _env_float("AGENT_MAX_WALL_SECONDS", 1800.0),
            "max_llm_reviews": _env_int("SAFETY_MAX_LLM_REVIEWS", 6),
            "max_retries": _env_int("AGENT_MAX_RETRIES", 12),
        }
        params.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**params)

    # ── time ───────────────────────────────────────────────────────────

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def expired(self) -> bool:
        return self.max_wall_seconds > 0 and self.elapsed >= self.max_wall_seconds

    # ── consumption ────────────────────────────────────────────────────

    def allow_steps(self, count: int = 1) -> bool:
        if self.expired:
            return False
        return self.steps + count <= self.max_steps

    def consume_steps(self, count: int = 1) -> None:
        self.steps += count

    def allow_wave(self) -> bool:
        if self.expired:
            return False
        return self.waves + 1 <= self.max_waves

    def consume_wave(self) -> None:
        self.waves += 1

    def allow_replan(self) -> bool:
        if self.expired:
            return False
        return self.replans < self.max_replans

    def consume_replan(self) -> None:
        self.replans += 1

    def allow_llm_review(self) -> bool:
        return self.llm_reviews < self.max_llm_reviews

    def consume_llm_review(self) -> None:
        self.llm_reviews += 1

    def allow_retry(self) -> bool:
        """Retries are capped per run so a pathological loop cannot spin."""
        if self.expired:
            return False
        return self.retries < self.max_retries

    def consume_retry(self) -> None:
        self.retries += 1

    # ── reporting ──────────────────────────────────────────────────────

    def exceeded(self) -> list[str]:
        """Return human-readable reasons the budget is exhausted."""
        reasons: list[str] = []
        if self.steps >= self.max_steps:
            reasons.append(f"步骤数达到上限 {self.max_steps}")
        if self.waves >= self.max_waves:
            reasons.append(f"执行波次达到上限 {self.max_waves}")
        if self.replans >= self.max_replans:
            reasons.append(f"补救重规划达到上限 {self.max_replans}")
        if self.expired:
            reasons.append(f"运行时长达到上限 {self.max_wall_seconds:.0f}s")
        return reasons

    def snapshot(self) -> dict:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "waves": self.waves,
            "max_waves": self.max_waves,
            "replans": self.replans,
            "max_replans": self.max_replans,
            "llm_reviews": self.llm_reviews,
            "max_llm_reviews": self.max_llm_reviews,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "elapsed_seconds": round(self.elapsed, 2),
            "max_wall_seconds": self.max_wall_seconds,
            "exhausted": self.exceeded(),
        }
