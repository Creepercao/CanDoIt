"""
Shared utilities for API routers: output directories, SSE formatting, HTML saving.
"""
import json
import re as _re
import uuid
import logging
from pathlib import Path

OUTPUTS_DIR = Path(__file__).parent.parent.parent / "outputs"
SKILL_HTML_DIR = OUTPUTS_DIR / "skills"

_HTML_DETECT_RE = _re.compile(r"<!DOCTYPE\s+html|<html[\s>]", _re.IGNORECASE)

logger = logging.getLogger("api")


def sse(event: str, data: dict) -> str:
    """Build an SSE frame (event + data)."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def resolve_output_url(url: str) -> Path | None:
    """Resolve an /outputs URL safely inside OUTPUTS_DIR."""
    if not url.startswith("/outputs/"):
        return None
    rel = url.removeprefix("/outputs/").lstrip("/\\")
    candidate = (OUTPUTS_DIR / rel).resolve()
    root = OUTPUTS_DIR.resolve()
    if candidate == root or root not in candidate.parents:
        return None
    return candidate


def save_skill_html(skill_name: str, content: str, title: str = "") -> dict:
    """Save HTML content from a skill worker to outputs/skills/."""
    file_id = uuid.uuid4().hex[:12]
    safe_name = _re.sub(r"[^\w\-]", "_", skill_name)
    filename = f"{safe_name}_{file_id}.html"
    filepath = SKILL_HTML_DIR / filename

    if not title:
        title_match = _re.search(r"<title>(.*?)</title>", content, _re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

    filepath.write_text(content, encoding="utf-8")
    html_url = f"/outputs/skills/{filename}"

    logger.info(f"Saved skill HTML: {skill_name} -> {filepath}")
    return {
        "html_url": html_url,
        "file_path": str(filepath),
        "title": title or f"{skill_name} output",
        "source_skill": skill_name,
    }
