"""Skill base class — the standard interface for skill modules.

Each skill module under ``backend/skills/`` exposes a single module-level
``SKILL`` instance. The SkillRegistry discovers these at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional


@dataclass
class Skill:
    """A self-contained, auto-discovered agent module.

    Skills can define worker functions (graph nodes), tools, and prompt
    contributions. They are discovered automatically from the skills package
    and integrated into the LangGraph multi-agent graph at startup.
    """

    # ── Identity ──
    name: str
    """Unique identifier, e.g. 'translator'. Used as agent type in task routing."""

    display_name: str
    """Human-readable label, e.g. 'Translator'."""

    description: str
    """One-line summary of what this skill does (shown in supervisor prompt)."""

    emoji: str = "🔧"
    """Icon shown in frontend and SSE events."""

    enabled: bool = True
    """If False, the skill is not loaded into the graph or shown to the supervisor."""

    # ── Behaviour ──
    worker: Optional[Callable[..., Awaitable[dict]]] = None
    """Async function (state: dict) -> dict. Registered as a LangGraph node.

    Must return a dict whose keys are written back to AgentState.
    Convention for new skills: return ``{"skill_outputs": {self.name: [result]}}``.
    """

    tools: dict[str, Callable] = field(default_factory=dict)
    """Callables this skill exposes (e.g. for use by other agents)."""

    depends_on: list[str] = field(default_factory=list)
    """Agent type names that must complete BEFORE this skill runs.

    Empty list = independent (dispatched in parallel from supervisor).
    Non-empty = inserted into the chain after the last dependency.
    """

    prompt_contribution: str = ""
    """Text appended to the supervisor prompt so the LLM knows this agent exists.

    If empty, a default line is generated from name + description + depends_on.
    """

    # ── State ──
    result_key: str = ""
    """State key this worker writes results into.

    Defaults to ``{name}_results``. New skills should use ``skill_outputs``
    (the catch-all dict) instead of polluting the top-level state namespace.
    """

    _package_meta: Optional[dict] = field(default=None, repr=False)
    """Internal metadata for package-based skills (install path, version, etc.).
    None for Python module skills, a dict for installed standard skill packages.
    """

    def __post_init__(self):
        if not self.result_key:
            self.result_key = f"{self.name}_results"

    # ── Derived ──

    @property
    def node_name(self) -> str:
        """LangGraph node name for this skill's worker."""
        return f"{self.name}_worker"

    @property
    def is_independent(self) -> bool:
        """True if this skill has no upstream dependencies (parallel dispatch)."""
        return len(self.depends_on) == 0
