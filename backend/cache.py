"""Simple in-memory cache for supervisor routing decisions."""
import time
from typing import Any

_cache: dict[str, tuple[float, Any]] = {}


async def get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry:
        expires, value = entry
        if expires == 0 or time.time() < expires:
            return value
        del _cache[key]
    return None


async def set(key: str, value: Any, ttl: int = 300):
    expires = time.time() + ttl if ttl > 0 else 0
    _cache[key] = (expires, value)


async def delete(key: str):
    _cache.pop(key, None)


def hash_key(text: str) -> str:
    return str(hash(text))
