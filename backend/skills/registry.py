"""SkillRegistry — discovers, validates, and queries skill modules.

Provides helpers consumed by graph.py (node registration, routing maps)
and main.py (labels, SSE tracking, API responses).
"""

from __future__ import annotations

import importlib
import pkgutil
import logging
from pathlib import Path
from typing import Callable, Optional

from backend.skills.base import Skill

logger = logging.getLogger("skills")


def _merge_skill_outputs(existing: dict, incoming: dict) -> dict:
    """Reducer for ``skill_outputs`` in AgentState.

    Merges two dicts by concatenating lists under matching keys.
    Used as the ``Annotated[dict, reducer]`` function so that parallel
    Send branches accumulate instead of overwriting.
    """
    merged = {**existing}
    for key, value in incoming.items():
        if key in merged:
            merged[key] = merged[key] + list(value)
        else:
            merged[key] = list(value)
    return merged


class SkillRegistry:
    """Auto-discovers and manages skill modules under ``backend.skills``.

    Usage::

        from backend.skills.registry import skill_registry

        skill_registry.discover()
        for skill in skill_registry.get_enabled().values():
            print(skill.display_name)
    """

    def __init__(self, skills_package: str = "backend.skills"):
        self._package = skills_package
        self._skills: dict[str, Skill] = {}
        self._discovered = False
        self._builtins_registered = False

    # ── Discovery ──────────────────────────────────────────────

    def discover(self) -> None:
        """Import all modules under the skills package and register their SKILL instances.

        Idempotent — subsequent calls are no-ops.
        """
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
                    logger.info(
                        f"Discovered skill: {skill.name} ({skill.display_name}) "
                        f"[{'independent' if skill.is_independent else 'chain: ' + ','.join(skill.depends_on)}]"
                    )
                else:
                    logger.debug(f"Module '{name}' has no SKILL instance — skipping")
            except Exception as e:
                logger.error(f"Failed to load skill module '{name}': {e}")

        self._discovered = True
        logger.info(f"Skill discovery complete: {len(self._skills)} skills found")

    # ── Built-in registration ────────────────────────────────────

    def register_builtins(self, skills: list[Skill]) -> None:
        """Register built-in agent configurations.

        Must be called BEFORE ``get_chain_agents()`` or graph-building so that
        built-in agents participate in chain ordering and routing rules.
        Idempotent — subsequent calls are no-ops.
        """
        if self._builtins_registered:
            return
        for skill in skills:
            if skill.name not in self._skills:
                self.register(skill)
        self._builtins_registered = True
        logger.info(f"Registered {len(skills)} built-in agent configs")

    # ── Registration ───────────────────────────────────────────

    def register(self, skill: Skill) -> None:
        """Register a skill (auto-discovered or manually added)."""
        if skill.name in self._skills:
            logger.warning(f"Skill '{skill.name}' already registered; overwriting.")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        """Remove a skill by name."""
        self._skills.pop(name, None)

    # ── Query ──────────────────────────────────────────────────

    def get(self, name: str) -> Optional[Skill]:
        """Get a skill by name, or None."""
        return self._skills.get(name)

    def get_all(self) -> dict[str, Skill]:
        """Return all registered skills (including disabled)."""
        return dict(self._skills)

    def get_enabled(self) -> dict[str, Skill]:
        """Return only enabled skills."""
        return {k: v for k, v in self._skills.items() if v.enabled}

    # ── Graph-building helpers ─────────────────────────────────

    def _has_worker(self, skill: Skill) -> bool:
        """Check whether a skill has a resolvable worker function.

        Returns True if the skill defines its own worker OR is a built-in
        agent whose worker lives in ``builtin_workers.WORKER_MAP``.
        """
        if skill.worker is not None:
            return True
        # Check built-in worker map (lazy import to avoid circular deps)
        try:
            from backend.agents.builtin_workers import WORKER_MAP
            return skill.name in WORKER_MAP
        except ImportError:
            return False

    def get_node_funcs(self) -> dict[str, Callable]:
        """Return ``{node_name: worker_function}`` for all enabled skills with workers."""
        result: dict[str, Callable] = {}
        for skill in self.get_enabled().values():
            if skill.worker is not None:
                result[skill.node_name] = skill.worker
        return result

    def get_node_for_agent(self) -> dict[str, str]:
        """Map agent type → node name for all enabled skills with workers."""
        result: dict[str, str] = {}
        # Built-in agents: use their node_name from the Skill config
        for skill in self.get_enabled().values():
            if self._has_worker(skill):
                result[skill.name] = skill.node_name
        return result

    def get_agent_labels(self) -> dict[str, str]:
        """Return ``{node_name: display_label}`` for SSE / frontend use."""
        result: dict[str, str] = {}
        for skill in self.get_enabled().values():
            if skill.worker is not None:
                result[skill.node_name] = f"{skill.emoji} {skill.display_name}"
        return result

    def get_independent_agents(self) -> set[str]:
        """Return set of agent-type names for independent (parallel dispatch) agents.

        Includes both built-in agents and skills whose workers are resolvable
        and whose ``depends_on`` is empty.
        """
        return {
            s.name for s in self.get_enabled().values()
            if self._has_worker(s) and s.is_independent
        }

    def get_chain_agents(self) -> list[str]:
        """Return ordered list of agent types in the sequential chain.

        The base chain is built dynamically from all registered agents that have
        ``depends_on`` relationships (via topological sort), so the core
        ``research → analyst → chart`` order is derived rather than hardcoded.

        Skills with ``depends_on`` are inserted after their last dependency.
        Insertion order: skills with fewer deps first (deterministic).
        """
        chain = self._build_base_chain()

        enabled = self.get_enabled()
        skill_entries: list[tuple[str, list[str]]] = []
        for skill in enabled.values():
            if self._has_worker(skill) and not skill.is_independent:
                # Only insert skills that aren't already in the base chain
                if skill.name not in chain:
                    skill_entries.append((skill.name, list(skill.depends_on)))

        # Sort by dependency count (fewer → inserted first)
        skill_entries.sort(key=lambda x: len(x[1]))

        for name, deps in skill_entries:
            insert_pos = -1
            for dep in deps:
                try:
                    pos = chain.index(dep)
                    insert_pos = max(insert_pos, pos)
                except ValueError:
                    pass
            if insert_pos >= 0:
                chain.insert(insert_pos + 1, name)
            else:
                # No dependency found in chain — prepend
                chain.insert(0, name)

        return chain

    def _build_base_chain(self) -> list[str]:
        """Build base chain from registered agents with dependency relationships.

        Uses topological sort on the dependency graph formed by all registered
        agents (both built-in and skill).  Agents with no deps and no dependents
        are excluded — they are independent workers, not chain members.
        """
        enabled = self.get_enabled()

        # Collect all agents that participate in dependency relationships
        deps_graph: dict[str, set[str]] = {}  # name → set of depends_on
        for skill in enabled.values():
            if skill.depends_on:
                deps_graph[skill.name] = set(skill.depends_on)

        if not deps_graph:
            # Fallback — ensure the classic chain exists
            return ["research", "analyst", "chart"]

        # Collect all nodes (agents named in deps_graph or as dependencies)
        all_nodes: set[str] = set(deps_graph.keys())
        for deps in deps_graph.values():
            all_nodes.update(deps)

        # Topological sort (Kahn's algorithm)
        in_degree: dict[str, int] = {n: 0 for n in all_nodes}
        for n, deps in deps_graph.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[n] = in_degree.get(n, 0) + 1

        queue = [n for n in all_nodes if in_degree.get(n, 0) == 0]
        result: list[str] = []

        while queue:
            n = queue.pop(0)
            result.append(n)
            # Find agents that depend on n
            for other in all_nodes:
                if n in deps_graph.get(other, set()):
                    in_degree[other] -= 1
                    if in_degree[other] == 0:
                        queue.append(other)

        # If topological sort didn't cover all nodes, there's a cycle.
        # Append remaining nodes at the end and log a warning.
        remaining = [n for n in all_nodes if n not in result]
        if remaining:
            logger.warning(
                f"Circular dependency detected among: {remaining}. "
                f"Appending to end of chain."
            )
            result.extend(remaining)

        return result

    def get_result_keys(self) -> list[str]:
        """Return all state keys that workers may write results into."""
        keys: list[str] = ["skill_outputs"]
        for skill in self.get_enabled().values():
            if skill.worker is not None and skill.result_key not in keys:
                keys.append(skill.result_key)
        return keys

    # ── Supervisor prompt ──────────────────────────────────────

    def build_skills_section(self) -> str:
        """Build the 'Additional skill agents:' section for the supervisor prompt.

        Only includes non-built-in skills — built-in agents are already listed
        in the hardcoded portion of the supervisor template.
        """
        # Names of built-in agents (already in the prompt template)
        try:
            from backend.agents.builtin_workers import WORKER_MAP as _BW
            _BUILTIN_NAMES = set(_BW.keys())
        except ImportError:
            _BUILTIN_NAMES = set()

        lines: list[str] = []
        for skill in self.get_enabled().values():
            if not self._has_worker(skill):
                continue
            if skill.name in _BUILTIN_NAMES:
                continue  # built-in agents are already in the template
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

    def build_routing_rules(self) -> str:
        """Build dynamic routing rules from all enabled skills + built-in rules.

        Generated rules include trigger-based dispatch instructions for each
        enabled package skill, followed by the static built-in agent rules.
        Rule numbering is consistent regardless of which skills are enabled.
        """
        lines: list[str] = []
        rule_num = 0

        # Dynamic rules from enabled skills that have worker functions
        for skill in self.get_enabled().values():
            if not self._has_worker(skill):
                continue

            triggers = []
            if skill._package_meta:
                triggers = skill._package_meta.get("triggers", [])
            if not isinstance(triggers, list):
                triggers = []

            if triggers:
                trigger_str = "、".join(triggers[:5])
                rule_num += 1
                dep_hint = ""
                if skill.depends_on:
                    dep_hint = f"（需要先经过 {', '.join(skill.depends_on)}）"
                lines.append(
                    f'{rule_num}. 用户要 {trigger_str} → 必须分派给 "{skill.name}"。'
                    f"{dep_hint} 禁止用文字回复。"
                )

        # Static built-in rules (always present)
        lines.append(f"{rule_num + 1}. 用户要 数据统计图表（柱状图/折线/饼图）→ 必须用 research→analyst→chart 链路。")
        lines.append(f'{rule_num + 2}. 用户要 图片/照片/插画 → 必须分派给 "image_gen"。')
        lines.append(f'{rule_num + 3}. 用户要 视频/动画 → 必须分派给 "video_gen"。')
        lines.append(f'{rule_num + 4}. 用户要 写代码/编程 → 必须分派给 "code"。')
        lines.append(f"{rule_num + 5}. 只有纯闲聊（问候、无产出的简单问题）才用 direct_response。")

        return "\n".join(lines)

    # ── Validation ──────────────────────────────────────────────

    def validate_dependencies(self) -> list[str]:
        """Check all registered agents for missing or circular dependencies.

        Returns a list of human-readable warning messages.
        """
        warnings: list[str] = []
        all_names = set(self._skills.keys())

        for skill in self._skills.values():
            if not skill.enabled:
                continue
            for dep in skill.depends_on:
                if dep not in all_names:
                    warnings.append(
                        f"Skill '{skill.name}': dependency '{dep}' is not registered. "
                        f"'{skill.name}' will be placed at chain entry."
                    )
                elif dep in all_names:
                    dep_skill = self._skills.get(dep)
                    if dep_skill and not dep_skill.enabled:
                        warnings.append(
                            f"Skill '{skill.name}': dependency '{dep}' is disabled. "
                            f"'{skill.name}' may not receive its expected input."
                        )

        return warnings

    # ── Serialization ──────────────────────────────────────────

    def to_api_list(self) -> list[dict]:
        """Return skill metadata suitable for the ``/api/skills`` endpoint."""
        result: list[dict] = []
        for skill in self._skills.values():
            is_package = skill._package_meta is not None
            entry = {
                "name": skill.name,
                "display_name": skill.display_name,
                "description": skill.description,
                "emoji": skill.emoji,
                "enabled": skill.enabled,
                "has_worker": skill.worker is not None,
                "depends_on": skill.depends_on,
                "is_independent": skill.is_independent,
                "node_name": skill.node_name,
                "tool_count": len(skill.tools),
                "is_package": is_package,
                "has_readme": is_package,  # package skills always have SKILL.md
            }
            if is_package:
                entry["package_version"] = skill._package_meta.get("version", "")
            result.append(entry)
        return result

    def get_readme(self, name: str) -> Optional[str]:
        """Get the full SKILL.md content for a skill, if available."""
        from backend.skills.package_installer import get_readme as _get_readme
        return _get_readme(name)


# ── Singleton ──

skill_registry = SkillRegistry()
