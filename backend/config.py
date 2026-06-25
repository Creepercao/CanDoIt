"""Load model provider config from API.json."""
import json
from pathlib import Path
from typing import Any
from pydantic import BaseModel


class ProviderConfig(BaseModel):
    name: str
    base_url: str
    apikey: str


def load_providers() -> list[ProviderConfig]:
    api_json = Path(__file__).parent.parent / "API.json"
    if not api_json.exists():
        return []

    with open(api_json, "r", encoding="utf-8") as f:
        raw = f.read()

    # API.json uses JS-like loose syntax — extract values
    providers: list[ProviderConfig] = []
    # Try standard JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                providers.append(ProviderConfig(**item))
        elif isinstance(data, dict):
            providers.append(ProviderConfig(**data))
        return providers
    except json.JSONDecodeError:
        pass

    # Fallback: parse loose JS-like format
    import re
    # Split by object boundaries
    blocks = re.split(r'\}\s*\{', raw.strip().strip('[]').strip())
    for block in blocks:
        block = block.strip().strip('{').strip('}').strip()
        name_match = re.search(r'name\s*:\s*"([^"]+)"', block)
        url_match = re.search(r'base_url\s*:\s*"([^"]+)"', block)
        key_match = re.search(r'APIkey\s*:\s*"([^"]+)"', block)
        if url_match and key_match:
            providers.append(ProviderConfig(
                name=name_match.group(1) if name_match else "unknown",
                base_url=url_match.group(1),
                apikey=key_match.group(1),
            ))
    return providers


PROVIDERS = load_providers()
