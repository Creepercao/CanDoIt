"""Web search — Tavily agent-optimised search (preferred) or Bing scraping (fallback).

Tavily integration follows the official agent pattern:
- ``search(search_depth="advanced", include_answer="advanced", include_raw_content="markdown")``
  for comprehensive results with AI answer + full page content
- ``get_search_context()`` — purpose-built for LLM agents: returns an optimised context
  string capped by token budget (no manual truncation needed)
- ``extract(urls)`` — clean markdown extraction from known URLs (replaces trafilatura scraping)
"""

from __future__ import annotations

import re
import logging
from typing import Any
from urllib.parse import unquote

import httpx

from backend.config import get_provider_by_type

logger = logging.getLogger("search")


# ── Public API ─────────────────────────────────────────────────────────


async def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web, returning ``[{title, url, snippet, raw_content?}, ...]``.

    Uses Tavily (agent-optimised) when a ``type: search`` provider is configured.
    Falls back to Bing HTML scraping otherwise.
    """
    provider = get_provider_by_type("search")
    if provider and provider.api_key:
        return await _tavily_search(query, max_results, provider.api_key)

    return await _bing_search(query, max_results)


async def get_search_context(query: str, max_tokens: int = 4000) -> str:
    """Return an LLM-optimised context string for *query*.

    This is Tavily's purpose-built agent method — it formats search results
    specifically for LLM consumption with smart token budgeting, deduplication,
    and content ranking.  Use this in agent prompts instead of manually
    concatenating search snippets.

    Returns an empty string when no search provider is configured.
    """
    provider = get_provider_by_type("search")
    if not provider or not provider.api_key:
        return ""

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=provider.api_key)
        context = client.get_search_context(
            query=query,
            max_tokens=max_tokens,
            search_depth="advanced",
        )
        return context if isinstance(context, str) else ""
    except Exception as e:
        logger.warning(f"Tavily get_search_context error: {e}")
        return ""


async def extract_urls(urls: list[str], fmt: str = "markdown") -> dict:
    """Extract clean content from *urls* via Tavily's extract endpoint.

    Returns the raw Tavily response dict with ``results`` + ``failed_results`` keys.
    Each successful result has ``url``, ``raw_content`` (markdown / text), and
    ``images`` (if available).
    """
    provider = get_provider_by_type("search")
    if not provider or not provider.api_key:
        return {"results": [], "failed_results": urls}

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=provider.api_key)
        return client.extract(
            urls=urls,
            format=fmt,
            extract_depth="advanced",
        )
    except Exception as e:
        logger.warning(f"Tavily extract error: {e}")
        return {"results": [], "failed_results": urls}


async def tavily_answer(query: str) -> str:
    """Quick AI-generated answer from Tavily (Q&A mode)."""
    provider = get_provider_by_type("search")
    if not provider or not provider.api_key:
        return ""

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=provider.api_key)
        return client.qna_search(query=query)
    except Exception as e:
        logger.warning(f"Tavily qna_search error: {e}")
        return ""


# ── Tavily agent-optimised search ──────────────────────────────────────


async def _tavily_search(
    query: str,
    max_results: int,
    api_key: str,
) -> list[dict[str, str]]:
    """Agent-optimised Tavily search — advanced depth, AI answer, full markdown content.

    Returns enriched dicts with ``raw_content`` (full page markdown) and
    ``score`` (relevance 0-1) in addition to title/url/snippet.
    Also appends a synthetic result containing the AI-generated ``answer``.
    """
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            search_depth="advanced",       # comprehensive — multiple semantically relevant snippets per URL
            max_results=max(max_results, 10),
            include_answer="advanced",     # detailed AI-synthesised answer
            include_raw_content="markdown", # full page content in markdown (replaces manual scraping)
            topic="general",
        )

        results: list[dict[str, str]] = []

        # Main results with raw_content
        for r in response.get("results", []):
            entry = {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "raw_content": r.get("raw_content", ""),
                "score": str(r.get("score", 0)),
            }
            results.append(entry)

        # Prepend the AI-generated answer as a synthetic "result" so downstream
        # consumers (research_worker, data_scraper) can use it for synthesis.
        answer = response.get("answer", "")
        if answer:
            results.insert(0, {
                "title": "AI 综合回答",
                "url": "",
                "snippet": answer,
                "raw_content": "",
                "score": "1.0",
                "is_answer": "true",
            })

        logger.info(
            f"Tavily: {len(results)} results (incl. answer) for '{query[:60]}' "
            f"({len([r for r in results if r.get('raw_content')])} with full content)"
        )
        return results

    except Exception as e:
        logger.warning(f"Tavily error: {e} — falling back to Bing")
        return await _bing_search(query, max_results)


# ── Bing (fallback) ────────────────────────────────────────────────────


async def _bing_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """HTML-scrape cn.bing.com — used when no search provider is configured."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://cn.bing.com/search",
                params={"q": query, "count": str(max_results)},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning(f"Bing returned {resp.status_code}")
                return _fallback()

            text = resp.text
            results: list[dict[str, str]] = []

            blocks = re.findall(
                r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                text, re.DOTALL,
            )

            for block in blocks[:max_results]:
                link = re.search(
                    r'<a[^>]*href\s*=\s*"([^"]+)"[^>]*>(.*?)</a>',
                    block, re.DOTALL,
                )
                if not link:
                    continue

                url = link.group(1)
                title = _clean(link.group(2))

                if not title or len(title) < 3:
                    continue

                snippet = ""
                sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
                if sn:
                    snippet = _clean(sn.group(1))
                else:
                    sn = re.search(
                        r'class="b_caption"[^>]*>(.*?)</div>',
                        block, re.DOTALL,
                    )
                    if sn:
                        snippet = _clean(sn.group(1))

                results.append({"title": title, "url": url, "snippet": snippet})

            if results:
                return results

            all_links = re.findall(
                r'<h2[^>]*>.*?<a[^>]*href\s*=\s*"(https?://[^"]+)"[^>]*>(.*?)</a>',
                text, re.DOTALL,
            )
            for url, title in all_links[:max_results]:
                t = _clean(title)
                if t and len(t) > 3 and not any(
                    s in url for s in ("bing.com", "microsoft.com", "go.microsoft.com")
                ):
                    results.append({"title": t, "url": url, "snippet": ""})

            return results[:max_results]

    except Exception as e:
        logger.warning(f"Bing error: {e}")
        return _fallback()


def _fallback() -> list[dict[str, str]]:
    return [{"title": "Search unavailable", "snippet": "Search failed.", "url": ""}]


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
