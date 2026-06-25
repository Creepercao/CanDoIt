"""Standard skill package loader — parses SKILL.md, builds workers, restores from disk."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import HumanMessage

from backend.models.provider import create_chat_model
from backend.skills.base import Skill

logger = logging.getLogger("skill.packages")

PACKAGES_DIR = Path(__file__).resolve().parent / "packages"
PACKAGES_DIR.mkdir(exist_ok=True)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_md(content: str) -> tuple[dict[str, Any], str]:
    """Parse SKILL.md into (frontmatter_dict, body_text)."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        logger.warning("SKILL.md has no YAML frontmatter")
        return {}, content

    fm_text = m.group(1)
    body = content[m.end():].strip()

    fm: dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: list[str] = []

    for line in fm_text.splitlines():
        if current_key and line.strip().startswith("- "):
            current_list.append(line.strip()[2:].strip().strip('"').strip("'"))
            continue
        if current_key and not line.startswith("  "):
            fm[current_key] = current_list
            current_key = None
            current_list = []

        kv = line.split(":", 1)
        if len(kv) < 2:
            continue
        key = kv[0].strip()
        value = kv[1].strip()
        if value == "":
            current_key = key
            current_list = []
        else:
            value = value.strip().strip('"').strip("'")
            if value.lower() in ("true", "false"):
                fm[key] = value.lower() == "true"
            elif value.isdigit():
                fm[key] = int(value)
            else:
                fm[key] = value

    if current_key:
        fm[current_key] = current_list
    return fm, body


def _build_skill_context(state: dict, skill_name: str) -> str:
    """Extract relevant context from agent state for the skill worker."""
    parts: list[str] = []
    if state.get("user_request"):
        parts.append(f"## User Request\n{state['user_request']}")

    research = state.get("research_results", [])
    if research:
        syn = research[-1].get("synthesis", "") if research else ""
        if syn:
            parts.append(f"## Research Results\n{syn[:3000]}")

    for key in ("analyst_results", "chart_results", "image_results"):
        items = state.get(key, [])
        if items:
            parts.append(f"## {key}\n{json.dumps(items, ensure_ascii=False)[:2000]}")

    skill_outputs = state.get("skill_outputs", {})
    if skill_outputs:
        for k, v in skill_outputs.items():
            if k != skill_name and v:
                parts.append(f"## Skill: {k}\n{json.dumps(v, ensure_ascii=False)[:1000]}")

    return "\n\n".join(parts) if parts else "No additional context available."


def _load_skill_assets(skill_dir: str) -> str:
    """Load reference docs and template assets from skill directory (max ~16KB)."""
    skill_path = Path(skill_dir)
    parts: list[str] = []
    total = 0

    refs_dir = skill_path / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.iterdir()):
            if ref_file.suffix in (".md", ".txt") and total < 16000:
                try:
                    text = ref_file.read_text(encoding="utf-8-sig")
                    snippet = text[:4000]
                    parts.append(f"### 参考: {ref_file.name}\n```\n{snippet}\n```")
                    total += len(snippet)
                except Exception as e:
                    logger.warning(f"Cannot read reference {ref_file}: {e}")

    assets_dir = skill_path / "assets"
    if assets_dir.is_dir():
        for asset_file in sorted(assets_dir.glob("*.html"), key=lambda f: f.stat().st_size):
            if total >= 16000:
                break
            try:
                text = asset_file.read_text(encoding="utf-8-sig")
                limit = min(6000, 16000 - total)
                snippet = text[:limit]
                parts.append(f"### 模板: {asset_file.name}\n```html\n{snippet}\n```")
                total += len(snippet)
            except Exception as e:
                logger.warning(f"Cannot read asset {asset_file}: {e}")

    return "\n\n".join(parts) if parts else ""


def make_standard_skill_worker(skill_name: str, skill_dir: str, prompt_template: str) -> callable:
    """Create a closure-based worker for a standard skill package."""

    skill_assets = _load_skill_assets(skill_dir)
    if skill_assets:
        logger.info(f"Skill '{skill_name}': loaded assets ({len(skill_assets)} chars)")

    async def worker(state: dict) -> dict:
        tasks = [t for t in state.get("tasks", []) if t.get("agent") == skill_name]
        if not tasks:
            return {"skill_outputs": {}}

        llm = create_chat_model(
            model_id=state.get("chat_model_id", ""),
            temperature=0.7, max_tokens=6144,
        )

        results: list[dict] = []
        for task in tasks:
            task_prompt = task.get("prompt", "")
            context = _build_skill_context(state, skill_name)

            prompt_parts = [
                f"You are the '{skill_name}' skill expert.",
                f"# Skill Instructions\n{prompt_template}",
            ]
            if skill_assets:
                prompt_parts.append(f"# Reference Materials & Templates\n{skill_assets}")
            prompt_parts.append(f"# Available Context\n{context}")
            prompt_parts.append(f"# User Request\n{task_prompt}")
            prompt_parts.append(
                "Generate the output following the skill instructions above. "
                "If the instructions say to generate an HTML file, output the complete HTML code."
            )
            system_prompt = "\n\n".join(prompt_parts)

            try:
                resp = await llm.ainvoke([HumanMessage(content=system_prompt)])
                content = resp.content if hasattr(resp, "content") else str(resp)
                results.append({"task": task_prompt, "result": content,
                               "model": state.get("chat_model_id", "")})
                logger.info(f"Standard skill '{skill_name}' completed ({len(content)} chars)")
            except Exception as e:
                logger.error(f"Standard skill '{skill_name}' error: {e}")
                results.append({"task": task_prompt, "error": str(e)})

        return {"skill_outputs": {skill_name: results}}

    return worker


def build_skill(skill_dir: str, frontmatter: dict[str, Any], body_text: str) -> Skill:
    """Construct a Skill from parsed SKILL.md."""
    name = frontmatter["name"]
    display_name = frontmatter.get("display_name", "").strip() or name.replace("-", " ").title()
    description = frontmatter.get("description", "")[:300]
    version = frontmatter.get("version", "0.0.0")

    meta_extra = frontmatter.get("metadata", {})
    emoji = "📦"
    if isinstance(meta_extra, dict) and "emoji" in meta_extra:
        emoji = meta_extra["emoji"]

    triggers = frontmatter.get("triggers", [])
    if not isinstance(triggers, list):
        triggers = []

    _package_meta = {
        "source": "package",
        "version": version,
        "skill_dir": skill_dir,
        "license": frontmatter.get("license", ""),
        "triggers": triggers,
        "metadata": meta_extra if isinstance(meta_extra, dict) else {},
    }

    worker_fn = make_standard_skill_worker(
        skill_name=name, skill_dir=skill_dir, prompt_template=body_text,
    )

    triggers_str = ", ".join(triggers[:5])
    prompt_contribution = (
        f"- {name}: {description}. "
        f"Standard skill (v{version}). "
        f"Triggers: {triggers_str}. "
        f"Runs independently."
    )

    return Skill(
        name=name,
        display_name=display_name,
        description=description,
        emoji=emoji,
        enabled=True,
        worker=worker_fn,
        depends_on=[],
        prompt_contribution=prompt_contribution,
        _package_meta=_package_meta,
    )


def restore_from_disk(registry) -> list[Skill]:
    """Scan packages/ directory for SKILL.md files and register them.
    Called at startup after skill_registry.discover().
    """
    restored: list[Skill] = []
    if not PACKAGES_DIR.exists():
        return restored

    for pkg_dir in sorted(PACKAGES_DIR.iterdir()):
        if not pkg_dir.is_dir() or pkg_dir.name.startswith("_"):
            continue
        skill_md = pkg_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            content = skill_md.read_text(encoding="utf-8-sig")
            fm, body = parse_skill_md(content)
            if not fm.get("name"):
                continue

            skill = build_skill(str(pkg_dir), fm, body)
            registry.register(skill)
            restored.append(skill)
            logger.info(f"Restored package skill: {skill.name} v{fm.get('version', '?')}")
        except Exception as e:
            logger.error(f"Failed to restore package skill from {pkg_dir}: {e}")

    return restored
