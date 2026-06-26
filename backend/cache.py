"""Redis cache layer for model list, supervisor decisions, session state."""
from __future__ import annotations

import json
import hashlib
import os
from typing import Any

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class Cache:
    """Simple async cache. Uses Redis if available (reads REDIS_URL env), else in-memory dict."""

    def __init__(self, redis_url: str = ""):
        self._redis = None
        self._memory: dict[str, tuple[float, Any]] = {}
        url = redis_url or os.environ.get("REDIS_URL", "")
        if REDIS_AVAILABLE and url:
            try:
                self._redis = aioredis.from_url(url, decode_responses=True)
            except Exception:
                self._redis = None

    async def get(self, key: str) -> Any | None:
        if self._redis:
            try:
                val = await self._redis.get(key)
                return json.loads(val) if val else None
            except Exception:
                pass
        # Fallback to memory
        import time
        entry = self._memory.get(key)
        if entry:
            expires, value = entry
            if expires == 0 or time.time() < expires:
                return value
            del self._memory[key]
        return None

    async def set(self, key: str, value: Any, ttl: int = 300):
        if self._redis:
            try:
                await self._redis.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
                return
            except Exception:
                pass
        import time
        expires = time.time() + ttl if ttl > 0 else 0
        self._memory[key] = (expires, value)

    async def delete(self, key: str):
        if self._redis:
            try:
                await self._redis.delete(key)
            except Exception:
                pass
        self._memory.pop(key, None)

    @staticmethod
    def hash_key(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:12]

    @property
    def available(self) -> bool:
        return self._redis is not None or True  # memory always available

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"


# Global cache instance
cache = Cache()
