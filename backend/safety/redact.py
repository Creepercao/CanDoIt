"""Secret redaction helpers.

Provider credentials live in ``API.json`` and are injected into every model
client.  They must never reach application logs, audit records, SSE payloads,
or the synthesizer prompt.  ``redact()`` is deliberately cheap because it runs
on every audited payload; ``scan_secrets()`` is the richer detector used by the
post-execution output review.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "«redacted»"

# ── Known credential shapes ────────────────────────────────────────────

_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\b")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
]

# ``api_key = "..."`` / ``"password": "..."`` style assignments, including the
# Chinese field names that show up in scraped pages and user paste-ins.
_KV_PATTERN = re.compile(
    r"""(?ix)
    (?P<key>
        \b(?:api[_-]?key|apikey|api[_-]?secret|access[_-]?token|auth[_-]?token|
            client[_-]?secret|secret[_-]?key|password|passwd|pwd)\b
        |(?:密码|口令|密钥|令牌|凭据)
    )
    \s*[:=：]\s*
    (?P<quote>["']?)
    (?P<value>[A-Za-z0-9_\-./+=]{8,})
    (?P=quote)
    """
)

_MASK_HEAD = 3


def _mask(value: str) -> str:
    """Return a short, non-reversible preview used as evidence."""
    value = value.strip()
    if len(value) <= _MASK_HEAD:
        return PLACEHOLDER
    return f"{value[:_MASK_HEAD]}…{PLACEHOLDER}"


def _kv_replace(match: re.Match[str]) -> str:
    return f"{match.group('key')}={PLACEHOLDER}"


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace credential-looking values with a placeholder.

    Accepts ``str``, ``dict``, ``list``/``tuple`` and scalars.  Depth is capped
    so a recursive structure cannot blow the stack.
    """
    if _depth > 8:
        return PLACEHOLDER
    if isinstance(value, str):
        text = value
        for _name, pattern in _TOKEN_PATTERNS:
            text = pattern.sub(PLACEHOLDER, text)
        text = _KV_PATTERN.sub(_kv_replace, text)
        return text
    if isinstance(value, dict):
        return {
            str(k) if not isinstance(k, str) else k: redact(v, _depth=_depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth=_depth + 1) for v in value]
    return value


def scan_secrets(text: str) -> list[dict]:
    """Return findings for credential-shaped substrings in *text*.

    Evidence never contains the full secret — only a masked preview.
    """
    if not isinstance(text, str) or not text:
        return []
    findings: list[dict] = []
    for name, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            findings.append({
                "id": f"secret:{name}",
                "category": "secret_exposure",
                "severity": "critical",
                "message": f"检测到疑似凭据泄露（{name}）",
                "evidence": _mask(match.group()),
                "offset": match.start(),
            })
    for match in _KV_PATTERN.finditer(text):
        findings.append({
            "id": f"secret:kv:{match.group('key').lower()}",
            "category": "secret_exposure",
            "severity": "high",
            "message": f"检测到明文凭据字段 {match.group('key')}",
            "evidence": _mask(match.group("value")),
            "offset": match.start(),
        })
    return findings


def has_secret(text: str) -> bool:
    """Fast boolean check used as a short-circuit in hot paths."""
    return bool(scan_secrets(text))
