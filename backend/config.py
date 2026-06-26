"""Load provider configuration from providers.yaml (or legacy API.json)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

logger = logging.getLogger("config")

_PROJECT_ROOT = Path(__file__).parent.parent


class ProviderConfig(BaseModel):
    """Single provider entry with a type tag for capability routing.

    ``type`` is one of:
    - ``llm``     — OpenAI-compatible chat / text-generation API
    - ``image``   — image generation endpoint (e.g. /images/generations)
    - ``video``   — video generation endpoint (e.g. /videos/generations)
    - ``search``  — search API (e.g. Tavily, SerpAPI) — no /models endpoint
    """

    name: str
    type: Literal["llm", "image", "video", "search"] = "llm"
    base_url: str = ""
    api_key: str = ""


# ── Loading ──────────────────────────────────────────────────────────


def _load_from_providers_yaml(yaml_path: Path) -> list[ProviderConfig]:
    """Parse the modern ``providers.yaml`` format."""
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw_list: list[dict] = []
    if isinstance(data, dict):
        raw_list = data.get("providers", [])
    elif isinstance(data, list):
        raw_list = data

    providers: list[ProviderConfig] = []
    for item in raw_list:
        # Normalise legacy key names
        if "APIkey" in item:
            item["api_key"] = item.pop("APIkey")
        if "apikey" in item:
            item["api_key"] = item.pop("apikey")
        providers.append(ProviderConfig(**item))

    return providers


def _load_from_legacy_api_json(json_path: Path) -> list[ProviderConfig]:
    """Fallback parser for the old ``API.json`` (JS-object syntax).

    Emits a deprecation warning — users should migrate to ``providers.yaml``.
    """
    logger.warning(
        "providers.yaml not found — falling back to legacy API.json. "
        "Please migrate to providers.yaml for multi-provider support."
    )

    with open(json_path, "r", encoding="utf-8") as f:
        raw = f.read()

    # Try standard JSON first
    try:
        data = json.loads(raw)
        items = data if isinstance(data, list) else [data]
        return [
            ProviderConfig(
                name=item.get("name", "unknown"),
                type="llm",
                base_url=item.get("base_url", ""),
                api_key=item.get("APIkey", item.get("apikey", item.get("api_key", ""))),
            )
            for item in items
        ]
    except json.JSONDecodeError:
        pass

    # Loose JS-object syntax
    import re

    providers: list[ProviderConfig] = []
    blocks = re.split(r"\}\s*\{", raw.strip().strip("[]").strip())
    for block in blocks:
        block = block.strip().strip("{").strip("}").strip()
        name_match = re.search(r'name\s*:\s*"([^"]+)"', block)
        url_match = re.search(r'base_url\s*:\s*"([^"]+)"', block)
        key_match = re.search(r'(?:APIkey|apikey|api_key)\s*:\s*"([^"]+)"', block)
        if url_match and key_match:
            providers.append(ProviderConfig(
                name=name_match.group(1) if name_match else "unknown",
                type="llm",
                base_url=url_match.group(1),
                api_key=key_match.group(1),
            ))
    return providers


def load_providers() -> list[ProviderConfig]:
    """Load all providers from ``providers.yaml``, falling back to
    ``API.json`` for backward compatibility."""
    yaml_path = _PROJECT_ROOT / "providers.yaml"
    legacy_path = _PROJECT_ROOT / "API.json"

    if yaml_path.exists():
        return _load_from_providers_yaml(yaml_path)

    if legacy_path.exists():
        return _load_from_legacy_api_json(legacy_path)

    return []


# ── Query helpers ────────────────────────────────────────────────────


def get_provider_by_type(provider_type: str) -> ProviderConfig | None:
    """Return the first provider matching *provider_type*, or ``None``."""
    for p in PROVIDERS:
        if p.type == provider_type:
            return p
    # Fallback: return first provider of any type
    return PROVIDERS[0] if PROVIDERS else None


# ── Module-level singleton ───────────────────────────────────────────

PROVIDERS: list[ProviderConfig] = load_providers()
