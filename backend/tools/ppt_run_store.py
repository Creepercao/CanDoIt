"""Persistent PPT generation run state.

Stores slide plans/fragments so a failed or low-quality slide can be regenerated
without rebuilding the whole deck.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

OUTPUTS_DIR = Path(__file__).parent.parent.parent / "outputs"
PPT_RUNS_DIR = OUTPUTS_DIR / "ppt_runs"


def new_deck_id() -> str:
    return uuid.uuid4().hex[:12]


def run_dir(deck_id: str) -> Path:
    safe = "".join(ch for ch in deck_id if ch.isalnum() or ch in "_-")[:64]
    path = PPT_RUNS_DIR / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_path(deck_id: str) -> Path:
    return run_dir(deck_id) / "manifest.json"


def read_manifest(deck_id: str) -> dict[str, Any]:
    path = manifest_path(deck_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_manifest(deck_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["deck_id"] = deck_id
    manifest["updated_at"] = time.time()
    path = manifest_path(deck_id)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def save_plan(deck_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    manifest = read_manifest(deck_id) or {
        "deck_id": deck_id,
        "created_at": time.time(),
        "slides": {},
        "artifacts": {},
    }
    manifest["plan"] = plan
    manifest["expected_slides"] = [
        int(slide.get("index") or idx + 1)
        for idx, slide in enumerate(plan.get("slides", []))
        if isinstance(slide, dict)
    ]
    return write_manifest(deck_id, manifest)


def save_slide(
    deck_id: str,
    slide_index: int,
    *,
    html: str = "",
    title: str = "",
    status: str = "ok",
    error: str = "",
) -> dict[str, Any]:
    path = run_dir(deck_id) / f"slide_{int(slide_index):02d}.html"
    if html:
        path.write_text(html, encoding="utf-8")

    manifest = read_manifest(deck_id)
    slides = manifest.setdefault("slides", {})
    slides[str(int(slide_index))] = {
        "index": int(slide_index),
        "title": title,
        "status": status,
        "error": error,
        "html_path": str(path),
        "updated_at": time.time(),
    }
    return write_manifest(deck_id, manifest)


def load_slide_html(deck_id: str, slide_index: int) -> str:
    path = run_dir(deck_id) / f"slide_{int(slide_index):02d}.html"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def save_artifact(deck_id: str, name: str, data: dict[str, Any]) -> dict[str, Any]:
    manifest = read_manifest(deck_id)
    manifest.setdefault("artifacts", {})[name] = data
    return write_manifest(deck_id, manifest)
