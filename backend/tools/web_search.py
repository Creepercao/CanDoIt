"""Web search tool — Tavily API (preferred) or Bing HTML scraping (fallback)."""

from __future__ import annotations

import re
import logging
from urllib.parse import unquote

import httpx

from backend.config import get_provider_by_type

logger = logging.getLogger("search")


async def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web and return ``[{title, url, snippet}, ...]``.

    Uses Tavily API if a ``type: search`` provider is configured, otherwise
    falls back to Bing China HTML scraping.
    """
    search_provider = get_provider_by_type("search")
    if search_provider and search_provider.api_key:
        return await _tavily_search(query, max_results, search_provider.api_key)

    return await _bing_search(query, max_results)


# ── Tavily ─────────────────────────────────────────────────────────────


async def _tavily_search(
    query: str,
    max_results: int,
    api_key: str,
) -> list[dict[str, str]]:
    """Search via Tavily API."""
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
        )

        results: list[dict[str, str]] = []
        for r in response.get("results", [])[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:300],
            })

        if results:
            logger.info(f"Tavily: {len(results)} results for '{query[:60]}'")
            return results

    except Exception as e:
        logger.warning(f"Tavily error: {e} — falling back to Bing")

    return await _bing_search(query, max_results)


# ── Bing (fallback) ────────────────────────────────────────────────────


async def _bing_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """HTML-scrape cn.bing.com search results."""
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
                sn = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
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

            # Broad fallback
            all_links = re.findall(
                r'<h2[^>]*>.*?<a[^>]*href\s*=\s*"(https?://[^"]+)"[^>]*>(.*?)</a>',
                text, re.DOTALL,
            )
            for url, title in all_links[:max_results]:
                t = _clean(title)
                if (
                    t and len(t) > 3
                    and not any(s in url for s in ("bing.com", "microsoft.com", "go.microsoft.com"))
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
