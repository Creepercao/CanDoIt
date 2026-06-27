"""Standard skill package installer — zip extraction, SKILL.md parsing, and
generic LLM-as-worker execution for skills that follow the spec:

  https://agentskills.io  (SKILL.md format)
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import HumanMessage

from backend.models.provider import create_chat_model
from backend.skills.base import Skill

logger = logging.getLogger("skill.packages")

# ── Paths ───────────────────────────────────────────────────────────
PACKAGES_DIR = Path(__file__).resolve().parent / "packages"
PACKAGES_DIR.mkdir(exist_ok=True)

PACKAGES_JSON = Path(__file__).resolve().parent.parent.parent / "skills_packages.json"

MAX_ZIP_SIZE = 200 * 1024 * 1024  # 200 MB


# ── YAML frontmatter parser (no pyyaml dependency) ───────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill_md(content: str) -> tuple[dict[str, Any], str]:
    """Parse ``SKILL.md`` content into (frontmatter_dict, body_text).

    Returns an empty dict for frontmatter if no ``---`` delimiters found.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        logger.warning("SKILL.md has no YAML frontmatter — treating entire file as body")
        return {}, content

    frontmatter_text = m.group(1)
    body = content[m.end():].strip()

    # Simple YAML parser for the flat frontmatter structure used by SKILL.md
    fm: dict[str, Any] = {}
    current_list_key: Optional[str] = None
    current_list: list[str] = []

    for line in frontmatter_text.splitlines():
        # List item (continuation)
        if current_list_key and line.strip().startswith("- "):
            current_list.append(line.strip()[2:].strip().strip('"').strip("'"))
            continue
        # End current list if indentation drops
        if current_list_key and not line.startswith("  "):
            fm[current_list_key] = current_list
            current_list_key = None
            current_list = []

        # Key: value line
        kv = line.split(":", 1)
        if len(kv) < 2:
            continue
        key = kv[0].strip()
        value = kv[1].strip()
        if value == "":
            # Could be the start of a list
            current_list_key = key
            current_list = []
        else:
            # Strip quotes
            value = value.strip().strip('"').strip("'")
            # Try to parse as bool/int
            if value.lower() in ("true", "false"):
                fm[key] = value.lower() == "true"
            elif value.isdigit():
                fm[key] = int(value)
            else:
                fm[key] = value

    if current_list_key:
        fm[current_list_key] = current_list

    return fm, body


# ── Zip extraction ──────────────────────────────────────────────────

def _path_safe(base: Path, target: Path) -> bool:
    """Ensure *target* does not escape *base* (path-traversal guard)."""
    try:
        base.resolve()
        target.resolve()
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def extract_skill_zip(zip_bytes: bytes) -> list[dict[str, Any]]:
    """Extract skill packages from a zip archive.

    Each top-level directory (or skill directory under a wrapper) containing
    a ``SKILL.md`` file is treated as one skill package.

    Returns a list of result dicts::

        {name, display_name, version, path: str, error?: str}
    """
    if len(zip_bytes) > MAX_ZIP_SIZE:
        return [{"name": "", "error": f"Zip too large ({len(zip_bytes)} bytes, max {MAX_ZIP_SIZE})"}]

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Find all SKILL.md paths in the archive
        skill_md_paths = [n for n in zf.namelist() if n.endswith("SKILL.md") and not n.startswith("__MACOSX")]

        if not skill_md_paths:
            return [{"name": "", "error": "No SKILL.md files found in archive"}]

        logger.info(f"Found {len(skill_md_paths)} SKILL.md files in zip")

        results: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for md_path in skill_md_paths:
            try:
                # Read SKILL.md to get the skill name
                md_content = zf.read(md_path).decode("utf-8-sig")
                fm, _body = parse_skill_md(md_content)

                name = fm.get("name", "").strip()
                if not name:
                    results.append({"name": "", "error": f"No 'name' in frontmatter of {md_path}"})
                    continue

                if name in seen_names:
                    continue
                seen_names.add(name)

                # Determine the skill's root directory in the zip
                # Use forward slashes consistently (zip internal format)
                md_parent_raw = Path(md_path).parent.as_posix()  # e.g. "AI_Animation-master/skills/flowchart"
                skill_root = md_parent_raw

                # Target directory
                target_dir = PACKAGES_DIR / name

                # Remove existing installation if present
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)

                # Extract all files under the skill root
                prefix = skill_root + "/"
                extracted = 0
                for member in zf.namelist():
                    if member.startswith("__MACOSX"):
                        continue
                    if member.startswith(prefix) and member != prefix:
                        rel = member[len(prefix):]
                        if not rel:
                            continue
                        dest = target_dir / rel
                        if not _path_safe(target_dir, dest):
                            logger.warning(f"Path traversal blocked: {member} → {dest}")
                            continue
                        if member.endswith("/"):
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            extracted += 1

                display_name = fm.get("display_name", name.replace("-", " ").title())
                version = fm.get("version", "0.0.0")

                results.append({
                    "name": name,
                    "display_name": display_name,
                    "version": version,
                    "path": str(target_dir),
                    "extracted_files": extracted,
                })
                logger.info(f"Extracted skill '{name}' v{version} → {target_dir} ({extracted} files)")

            except Exception as e:
                logger.error(f"Failed to extract skill from {md_path}: {e}")
                results.append({"name": "", "error": f"Extract failed for {md_path}: {e}"})

        return results


# ── Skill construction ──────────────────────────────────────────────

def _build_skill_context(state: dict, skill_name: str) -> str:
    """Extract relevant context from the agent state for the skill worker."""
    parts: list[str] = []

    # User request
    if state.get("user_request"):
        parts.append(f"## User Request\n{state['user_request']}")

    # Research results
    research = state.get("research_results", [])
    if research:
        syn = research[-1].get("synthesis", "") if research else ""
        if syn:
            parts.append(f"## Research Results\n{syn[:3000]}")

    # Existing outputs
    for key in ("analyst_results", "chart_results", "image_results"):
        items = state.get(key, [])
        if items:
            parts.append(f"## {key}\n{_fmt_results(items)[:2000]}")

    # Other skill outputs
    skill_outputs = state.get("skill_outputs", {})
    if skill_outputs:
        for k, v in skill_outputs.items():
            if k != skill_name and v:
                parts.append(f"## Skill: {k}\n{_fmt_results(v)[:1000]}")

    return "\n\n".join(parts) if parts else "No additional context available."


def _fmt_results(items: list) -> str:
    """Format a list of result dicts as a compact string."""
    if not items:
        return ""
    out: list[str] = []
    for item in items[:3]:
        if isinstance(item, dict):
            # Pick the most relevant value
            for key in ("result", "translated_text", "summary", "synthesis"):
                if key in item:
                    out.append(str(item[key])[:500])
                    break
            else:
                out.append(json.dumps(item, ensure_ascii=False)[:500])
        else:
            out.append(str(item)[:500])
    return "\n".join(out)


def make_standard_skill_worker(
    skill_name: str,
    skill_dir: str,
    prompt_template: str,
) -> callable:
    """Create a closure-based worker for a standard skill package.

    The worker reads the skill's ``SKILL.md`` instructions, combines them
    with the user's task and available state context, then invokes an LLM
    to generate the output.

    Parameters
    ----------
    skill_name : str
        Agent-type name used to filter tasks and key results.
    skill_dir : str
        Filesystem path to the extracted skill directory (for asset access).
    prompt_template : str
        The body text of ``SKILL.md`` — the instructions the LLM follows.
    """

    async def worker(state: dict) -> dict:
        tasks = [t for t in state.get("tasks", []) if t.get("agent") == skill_name]
        if not tasks:
            return {"skill_outputs": {}}

        logger.info(f"Standard skill worker '{skill_name}' processing {len(tasks)} task(s)")

        llm = create_chat_model(
            model_id=state.get("chat_model_id", ""),
            temperature=0.7,
            max_tokens=8192,
        )

        results: list[dict] = []
        for task in tasks:
            task_prompt = task.get("prompt", "")
            context = _build_skill_context(state, skill_name)

            system_prompt = (
                f"You are the '{skill_name}' skill expert. "
                f"Follow the instructions below to complete the user's request.\n\n"
                f"# Skill Instructions\n{prompt_template}\n\n"
                f"# Available Context\n{context}\n\n"
                f"# User Request\n{task_prompt}\n\n"
                f"Generate the output following the skill instructions above. "
                f"Output the result directly. "
                f"If the instructions say to generate an HTML file, output the complete HTML code."
            )

            try:
                resp = await llm.ainvoke([HumanMessage(content=system_prompt)])
                content = resp.content if hasattr(resp, "content") else str(resp)

                # ── Save HTML output to disk ──
                html_info = None
                if isinstance(content, str) and len(content) > 200:
                    if re.search(
                        r"<!DOCTYPE\s+html|<html[\s>]", content[:500], re.IGNORECASE
                    ):
                        try:
                            from backend.api import save_skill_html

                            saved = save_skill_html(
                                skill_name, content, title=task_prompt
                            )
                            html_info = saved
                            logger.info(
                                f"Saved HTML for skill '{skill_name}': {saved['html_url']}"
                            )
                        except Exception as html_err:
                            logger.warning(
                                f"Failed to save HTML for skill '{skill_name}': {html_err}"
                            )

                result_entry = {
                    "task": task_prompt,
                    "result": content,
                    "model": state.get("chat_model_id", ""),
                }
                if html_info:
                    result_entry["html_url"] = html_info["html_url"]
                    result_entry["html_title"] = html_info["title"]
                    result_entry["html_path"] = html_info["file_path"]
                results.append(result_entry)
                logger.info(f"Standard skill '{skill_name}' completed task: {task_prompt[:80]}...")
            except Exception as e:
                logger.error(f"Standard skill '{skill_name}' error: {e}")
                results.append({"task": task_prompt, "error": str(e)})

        return {"skill_outputs": {skill_name: results}}

    return worker


# ── Skill builder ───────────────────────────────────────────────────

def build_skill(skill_dir: str, frontmatter: dict[str, Any], body_text: str) -> Skill:
    """Construct a platform ``Skill`` from a standard skill package.

    Parameters
    ----------
    skill_dir : str
        Absolute path to the extracted skill directory.
    frontmatter : dict
        Parsed YAML frontmatter from ``SKILL.md``.
    body_text : str
        The markdown body (instructions for the LLM).
    """
    name = frontmatter["name"]
    display_name = frontmatter.get("display_name", "").strip() or name.replace("-", " ").title()
    description = frontmatter.get("description", "")[:300]
    version = frontmatter.get("version", "0.0.0")

    # Pick emoji from metadata if available
    emoji = "📦"
    meta_extra = frontmatter.get("metadata", {})
    if isinstance(meta_extra, dict) and "emoji" in meta_extra:
        emoji = meta_extra["emoji"]

    triggers = frontmatter.get("triggers", [])

    _package_meta = {
        "source": "package",
        "version": version,
        "skill_dir": skill_dir,
        "license": frontmatter.get("license", ""),
        "triggers": triggers if isinstance(triggers, list) else [],
        "metadata": meta_extra if isinstance(meta_extra, dict) else {},
    }

    worker_fn = make_standard_skill_worker(
        skill_name=name,
        skill_dir=skill_dir,
        prompt_template=body_text,
    )

    depends_on = frontmatter.get("depends_on", [])
    if not isinstance(depends_on, list):
        depends_on = []

    triggers_str = ", ".join(triggers[:5]) if isinstance(triggers, list) else ""
    if depends_on:
        dep_hint = f"Use AFTER {', '.join(depends_on)}."
    else:
        dep_hint = "Runs independently."

    prompt_contribution = (
        f"- {name}: {description}. "
        f"Standard skill (v{version}). "
        f"Triggers: {triggers_str}. "
        f"{dep_hint}"
    )

    return Skill(
        name=name,
        display_name=display_name,
        description=description,
        emoji=emoji,
        enabled=False,  # safety: disabled by default after install
        worker=worker_fn,
        depends_on=depends_on,
        prompt_contribution=prompt_contribution,
        _package_meta=_package_meta,
    )


# ── Persistence ─────────────────────────────────────────────────────

def _load_packages_json() -> dict:
    """Read ``skills_packages.json``, returning ``{name: info, ...}``."""
    if not PACKAGES_JSON.exists():
        return {}
    try:
        with open(PACKAGES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("packages", {})
    except Exception as e:
        logger.warning(f"Failed to read {PACKAGES_JSON}: {e}")
        return {}


def _save_packages_json(packages: dict[str, dict]) -> None:
    """Write ``skills_packages.json``."""
    try:
        with open(PACKAGES_JSON, "w", encoding="utf-8") as f:
            json.dump({"packages": packages}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to write {PACKAGES_JSON}: {e}")


def save_package_state(name: str, enabled: bool) -> None:
    """Update the ``enabled`` flag for one package in the JSON file."""
    packages = _load_packages_json()
    if name in packages:
        packages[name]["enabled"] = enabled
        _save_packages_json(packages)


def restore_from_disk(registry) -> list[Skill]:
    """Re-register all persisted package skills from ``skills_packages.json``.

    Called at startup after ``skill_registry.discover()``.
    Returns the list of re-registered ``Skill`` instances.

    Path resolution is **always** computed from ``PACKAGES_DIR / name`` —
    no absolute paths are stored or trusted.  This makes the JSON trivially
    portable across Docker, Windows, and macOS without any path flipping.
    """
    packages = _load_packages_json()
    restored: list[Skill] = []

    for name, info in packages.items():
        # Always compute the canonical path dynamically
        skill_dir = PACKAGES_DIR / name
        if not skill_dir.exists():
            # Rare edge case: the directory was renamed or moved manually
            stored = Path(info.get("skill_dir", ""))
            if stored.exists():
                skill_dir = stored
            else:
                logger.warning(
                    f"Package skill '{name}' directory missing — skipping "
                    f"(tried {skill_dir})"
                )
                continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            logger.warning(f"Package skill '{name}' SKILL.md missing — skipping")
            continue

        try:
            content = skill_md.read_text(encoding="utf-8-sig")
            fm, body = parse_skill_md(content)

            skill = build_skill(str(skill_dir), fm, body)
            skill.enabled = info.get("enabled", True)

            registry.register(skill)
            restored.append(skill)
            logger.info(
                f"Restored package skill: {name} v{info.get('version')} "
                f"[{'enabled' if skill.enabled else 'disabled'}]"
            )
        except Exception as e:
            logger.error(f"Failed to restore package skill '{name}': {e}")

    return restored


def install_from_zip(zip_bytes: bytes, registry) -> dict:
    """Full install pipeline: extract zip → build skills → register → persist.

    Returns a result dict suitable for the API response.
    """
    extracted = extract_skill_zip(zip_bytes)

    installed: list[dict] = []
    skipped: list[str] = []
    errors: list[str] = []

    for item in extracted:
        if item.get("error"):
            errors.append(item["error"])
            continue

        name = item["name"]
        skill_dir = item["path"]

        # Check for name collision with existing Python skills
        existing = registry.get(name)
        if existing and existing._package_meta is None:
            errors.append(f"'{name}': name conflicts with a built-in skill")
            # Clean up extracted files
            shutil.rmtree(skill_dir, ignore_errors=True)
            continue

        # Read SKILL.md and build the skill
        try:
            md_path = Path(skill_dir) / "SKILL.md"
            content = md_path.read_text(encoding="utf-8-sig")
            fm, body = parse_skill_md(content)

            skill = build_skill(skill_dir, fm, body)

            # Respect previous enabled state if reinstalling
            packages = _load_packages_json()
            if name in packages:
                skill.enabled = packages[name].get("enabled", False)

            registry.register(skill)
            installed.append({
                "name": name,
                "display_name": skill.display_name,
                "version": item["version"],
                "enabled": skill.enabled,
            })
        except Exception as e:
            logger.error(f"Failed to build skill '{name}': {e}")
            errors.append(f"'{name}': build failed — {e}")
            shutil.rmtree(skill_dir, ignore_errors=True)

    # Persist
    if installed:
        packages = _load_packages_json()
        for inst in installed:
            name = inst["name"]
            skill = registry.get(name)
            pkg_dir = str(PACKAGES_DIR / name)
            packages[name] = {
                "name": name,
                "version": inst["version"],
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "enabled": skill.enabled if skill else False,
                "skill_dir": pkg_dir,
            }
        _save_packages_json(packages)

    return {
        "installed": installed,
        "skipped": skipped,
        "errors": errors,
    }


def uninstall_package(name: str, registry) -> bool:
    """Remove a package skill: unregister, delete directory, update JSON.

    Returns True if the skill was uninstalled, False if not found.
    """
    skill = registry.get(name)
    if not skill:
        return False

    # Only allow uninstalling package-based skills
    if not skill._package_meta or skill._package_meta.get("source") != "package":
        logger.warning(f"Cannot uninstall '{name}': not a package skill")
        return False

    # Remove directory
    skill_dir = Path(skill._package_meta.get("skill_dir", ""))
    if skill_dir.exists():
        shutil.rmtree(skill_dir)

    # Unregister
    registry.unregister(name)

    # Update JSON
    packages = _load_packages_json()
    packages.pop(name, None)
    _save_packages_json(packages)

    logger.info(f"Uninstalled package skill: {name}")
    return True


def get_readme(name: str) -> Optional[str]:
    """Read the full ``SKILL.md`` content for a skill.

    Works for both Python skills (reads from the skills directory)
    and package skills (reads from the extracted directory).
    Returns None if the skill or its SKILL.md is not found.
    """
    # First try package directory
    pkg_dir = PACKAGES_DIR / name / "SKILL.md"
    if pkg_dir.exists():
        return pkg_dir.read_text(encoding="utf-8-sig")

    # Then try Python module directory (for code-based skills)
    py_dir = Path(__file__).resolve().parent / f"{name}.py"
    if py_dir.exists():
        return py_dir.read_text(encoding="utf-8")

    return None
