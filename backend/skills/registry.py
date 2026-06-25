"""SkillRegistry — discovers, validates, and queries skill modules."""
from __future__ import annotations

import importlib
import pkgutil
import logging
from pathlib import Path
from typing import Callable, Optional

from backend.skills.base import Skill

logger = logging.getLogger("skills")


def _merge_skill_outputs(existing: dict, incoming: dict) -> dict:
    """Reducer for skill_outputs in AgentState — merges dicts by concatenating lists."""
    merged = {**existing}
    for key, value in incoming.items():
        if key in merged:
            merged[key] = merged[key] + list(value)
        else:
            merged[key] = list(value)
    return merged


class SkillRegistry:
    """Auto-discovers and manages skill modules under backend.skills."""

    def __init__(self, skills_package: str = "backend.skills"):
        self._package = skills_package
        self._skills: dict[str, Skill] = {}
        self._discovered = False

    def discover(self) -> None:
        """Import all modules under the skills package and register their SKILL instances."""
        if self._discovered:
            return

        try:
            package = importlib.import_module(self._package)
            pkg_path = Path(package.__file__).parent
        except (ImportError, AttributeError) as e:
            logger.warning(f"Skills package '{self._package}' not found: {e}")
            self._discovered = True
            return

        for finder, name, is_pkg in pkgutil.iter_modules([str(pkg_path)]):
            if name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"{self._package}.{name}")
                skill = getattr(mod, "SKILL", None)
                if isinstance(skill, Skill):
                    self.register(skill)
                    logger.info(f"Discovered skill: {skill.name} ({skill.display_name})")
            except Exception as e:
                logger.error(f"Failed to load skill module '{name}': {e}")

        self._discovered = True
        logger.info(f"Skill discovery complete: {len(self._skills)} skills found")

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def get_all(self) -> dict[str, Skill]:
        return dict(self._skills)

    def get_enabled(self) -> dict[str, Skill]:
        return {k: v for k, v in self._skills.items() if v.enabled}

    # ── Graph-building helpers ──

    def get_node_funcs(self) -> dict[str, Callable]:
        """Return {node_name: worker_function} for all enabled skills."""
        return {s.node_name: s.worker
                for s in self.get_enabled().values() if s.worker is not None}

    def get_node_for_agent(self) -> dict[str, str]:
        """Map agent type → node name for all enabled skills."""
        return {s.name: s.node_name
                for s in self.get_enabled().values() if s.worker is not None}

    def get_agent_labels(self) -> dict[str, str]:
        """Return {node_name: display_label} for SSE / frontend."""
        return {s.node_name: f"{s.emoji} {s.display_name}"
                for s in self.get_enabled().values() if s.worker is not None}

    def get_independent_agents(self) -> set[str]:
        """Return set of agent-type names for independent (parallel dispatch) skills."""
        return {s.name for s in self.get_enabled().values()
                if s.worker is not None and s.is_independent}

    def build_skills_section(self) -> str:
        """Build the 'Additional skill agents:' section for the supervisor prompt."""
        lines: list[str] = []
        for skill in self.get_enabled().values():
            if skill.worker is None:
                continue
            if skill.prompt_contribution:
                lines.append(skill.prompt_contribution)
            else:
                dep_hint = ""
                if skill.depends_on:
                    dep_hint = f" Use AFTER {', '.join(skill.depends_on)}."
                else:
                    dep_hint = " Runs in parallel with other agents."
                lines.append(f"- {skill.name}: {skill.description}.{dep_hint}")

        if lines:
            return "Additional skill agents:\n" + "\n".join(lines) + "\n"
        return ""

    def to_api_list(self) -> list[dict]:
        """Return skill metadata for the /api/skills endpoint."""
        result: list[dict] = []
        for skill in self._skills.values():
            result.append({
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "emoji": skill.emoji,
                "enabled": skill.enabled,
                "has_worker": skill.worker is not None,
                "depends_on": skill.depends_on,
                "is_independent": skill.is_independent,
                "node_name": skill.node_name,
            })
        return result


skill_registry = SkillRegistry()
