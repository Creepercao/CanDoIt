"""Lightweight embedding client using the LLM provider's OpenAI-compatible
/v1/embeddings endpoint.  No additional model downloads needed — falls back
gracefully if the provider does not support embeddings.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Optional

import httpx

from backend.config import get_provider_by_type

logger = logging.getLogger("embedding")

# ── Configurable defaults ──
DEFAULT_EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5"
)
EMBEDDING_TIMEOUT = float(os.environ.get("EMBEDDING_TIMEOUT", "10"))

# ── Module-level state ──
_known_model: Optional[str] = None  # first successful model name


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity — no numpy dependency."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _embed_request(model: str, texts: list[str]) -> Optional[list[list[float]]]:
    """Call the LLM provider's /v1/embeddings endpoint.

    Returns a list of embedding vectors (one per input text), or None on failure.
    """
    provider = get_provider_by_type("llm")
    if not provider or not provider.base_url:
        logger.debug("No LLM provider configured for embeddings")
        return None

    base = provider.base_url.rstrip("/")
    url = f"{base}/embeddings"

    headers = {"Authorization": f"Bearer {provider.api_key}"}
    payload = {"model": model, "input": texts}

    try:
        async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data", [])
                # Sort by index to preserve input order
                items.sort(key=lambda x: x.get("index", 0))
                return [item["embedding"] for item in items]
            elif resp.status_code == 404:
                logger.debug(
                    f"Embedding model '{model}' not found on provider "
                    f"({resp.status_code})"
                )
                return None
            else:
                logger.warning(
                    f"Embedding request failed ({resp.status_code}): "
                    f"{resp.text[:200]}"
                )
                return None
    except httpx.TimeoutException:
        logger.warning(f"Embedding request timed out after {EMBEDDING_TIMEOUT}s")
        return None
    except Exception as e:
        logger.warning(f"Embedding request error: {e}")
        return None


async def get_embedding(
    text: str, model: str = ""
) -> Optional[list[float]]:
    """Get a single embedding vector for *text*.

    Uses the first embedding model that succeeds; caches the model name
    so subsequent calls skip the discovery step.
    """
    global _known_model

    target_model = model or _known_model or DEFAULT_EMBEDDING_MODEL
    results = await _embed_request(target_model, [text])

    if results is not None and results:
        if not _known_model:
            _known_model = target_model
            logger.info(f"Embedding model discovered: {_known_model}")
        return results[0]

    # If the default model fails and no explicit model was given, try a
    # common fallback (OpenAI-compatible proxies often expose this)
    if not model and target_model != "text-embedding-ada-002":
        logger.debug(
            f"Model '{target_model}' failed — trying fallback 'text-embedding-ada-002'"
        )
        results = await _embed_request("text-embedding-ada-002", [text])
        if results is not None and results:
            _known_model = "text-embedding-ada-002"
            logger.info(f"Embedding model fallback: {_known_model}")
            return results[0]

    return None


async def get_embeddings(
    texts: list[str], model: str = ""
) -> Optional[list[list[float]]]:
    """Get embedding vectors for multiple *texts*."""
    global _known_model

    target_model = model or _known_model or DEFAULT_EMBEDDING_MODEL
    results = await _embed_request(target_model, texts)

    if results is not None and len(results) == len(texts):
        if not _known_model:
            _known_model = target_model
        return results

    if not model and target_model != "text-embedding-ada-002":
        results = await _embed_request("text-embedding-ada-002", texts)
        if results is not None and len(results) == len(texts):
            _known_model = "text-embedding-ada-002"
            return results

    return None


def embedding_available() -> bool:
    """Return True if at least one embedding model has been discovered."""
    return _known_model is not None
