"""Skill base class — standard interface for skill modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional


@dataclass
class Skill:
    """A self-contained agent module, auto-discovered at startup."""

    name: str
    """Unique identifier, used as agent type in task routing."""

    display_name: str
    """Human-readable label, e.g. 'Translator'."""

    description: str
    """One-line summary shown in supervisor prompt."""

    emoji: str = "🔧"
    """Icon shown in frontend and SSE events."""

    enabled: bool = True
    """If False, the skill is not loaded."""

    worker: Optional[Callable[..., Awaitable[dict]]] = None
    """Async function (state: dict) -> dict. Registered as a LangGraph node."""

    depends_on: list[str] = field(default_factory=list)
    """Agent types that must complete BEFORE this skill. Empty = independent."""

    prompt_contribution: str = ""
    """Text appended to supervisor prompt so the LLM knows this agent exists."""

    _package_meta: Optional[dict] = field(default=None, repr=False)
    """Internal metadata for package-based skills (install path, version, etc.)."""

    def __post_init__(self):
        pass

    @property
    def node_name(self) -> str:
        """LangGraph node name for this skill's worker."""
        return f"{self.name}_worker"

    @property
    def is_independent(self) -> bool:
        """True if this skill has no upstream dependencies (parallel dispatch)."""
        return len(self.depends_on) == 0
