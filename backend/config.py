"""Load model provider config from API.json (lenient JS-like or standard JSON)."""
import json
import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    apikey: str


def load_providers() -> list[ProviderConfig]:
    api_json = Path(__file__).parent.parent / "API.json"
    if not api_json.exists():
        return []

    with open(api_json, "r", encoding="utf-8") as f:
        raw = f.read()

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

    # Fallback: parse JS-like format (unquoted keys, trailing commas)
    blocks = re.split(r'\}\s*,\s*\{', raw.strip().strip('[]').strip('{}').strip())
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
