"""Data scraper — Tavily extract (preferred) or trafilatura scraping (fallback).

When a ``type: search`` provider (Tavily) is configured:
- ``web_search()`` already returns ``raw_content`` (full page markdown) —
  no need for separate HTTP scraping
- ``get_search_context()`` provides LLM-optimised context for synthesis
- ``extract_urls()`` handles raw content extraction from known URLs in clean markdown

Without a search provider, falls back to Bing scraping + trafilatura extraction.
"""

from __future__ import annotations

import re
import json
import logging
import httpx
from typing import Any

import trafilatura

from backend.tools.web_search import web_search, get_search_context, extract_urls
from backend.config import get_provider_by_type

logger = logging.getLogger("scraper")

# Sports score sites accessible from China (used for Bing fallback)
SPORTS_SITES = [
    "sports.sina.com.cn", "sports.qq.com", "sports.163.com",
    "7m.com.cn", "zgzcw.com", "livescore.com", "bf.7m.com.cn",
    "live.zgzcw.com", "sports.cctv.com",
]
SKIP_SCRAPE = (
    "sogou.com/link", "sogou.com/web", "baidu.com/link",
    "baike.baidu.com", "baidu.com/s", "zhidao.baidu.com",
    "bing.com", "microsoft.com", "go.microsoft.com",
)


async def scrape_page(url: str, max_len: int = 8000) -> str:
    """Fetch and extract clean text from *url* using trafilatura (fallback only).

    When Tavily is configured, prefer ``extract_urls()`` for cleaner markdown output.
    """
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            if resp.status_code != 200:
                return ""
            html = resp.text
            if not html or len(html) < 100:
                return ""

        text = trafilatura.extract(
            html, include_comments=False, include_tables=True,
            no_fallback=False, favor_precision=True,
        )
        if text and len(text) > 100:
            return text[:max_len]

        text = trafilatura.extract(
            html, include_comments=False, include_tables=True,
            no_fallback=True,
        )
        if text and len(text) > 50:
            return text[:max_len]

        return ""
    except Exception as e:
        logger.debug(f"Scrape failed for {url[:60]}: {e}")
        return ""


async def search_and_scrape(query: str, llm, max_pages: int = 3) -> dict:
    """Multi-query search + content extraction + LLM synthesis.

    When Tavily is available, uses the agent-optimised pipeline:
    1. ``web_search()`` (advanced depth + raw_content + AI answer)
    2. ``get_search_context()`` for LLM-optimised synthesis input
    3. ``extract_urls()`` for additional URLs not covered by search raw_content

    Falls back to Bing + trafilatura scraping otherwise.

    Returns ``{synthesis, structured_data, sources}``.
    """
    has_tavily = _has_search_provider()

    # ── Generate search queries ──
    queries = [query]
    sports_kw = any(kw in query.lower() for kw in [
        "世界杯", "world cup", "比分", "score", "比赛", "match", "足球",
        "football", "soccer", "联赛", "league", "积分", "排名", "standings",
    ])
    if sports_kw:
        queries.append(query + " scores results")
        for site in SPORTS_SITES[:3]:
            queries.append(f"site:{site} {query[:40]}")

    # ── Search ──
    all_sources: list[dict] = []
    all_raw_texts: list[str] = []

    for q in queries[:3]:
        results = await web_search(q, max_results=5)
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            raw_content = r.get("raw_content", "")

            if not title:
                continue

            # Collect sources
            if url:
                all_sources.append({"title": title, "url": url})

            # With Tavily: use raw_content (full page markdown) directly
            if has_tavily and raw_content and len(raw_content) > 200:
                all_raw_texts.append(f"--- {title} ---\n{raw_content[:4000]}")
            elif has_tavily and snippet:
                all_raw_texts.append(f"[{title}]\n{snippet}")
            elif not has_tavily:
                # Without Tavily: scrape pages manually with trafilatura
                if url and not any(s in url for s in SKIP_SCRAPE):
                    text = await scrape_page(url, max_len=6000)
                    if text and len(text) > 200:
                        all_raw_texts.append(f"--- {title} ---\n{text}")
                if snippet and not all_raw_texts:
                    all_raw_texts.append(f"[{title}]\n{snippet}")

    # ── Deduplicate sources ──
    seen = set()
    unique_sources = []
    for s in all_sources:
        u = s.get("url", "")
        if u not in seen:
            seen.add(u)
            unique_sources.append(s)
    all_sources = unique_sources

    if not all_raw_texts:
        return {"synthesis": "No data found.", "structured_data": None, "sources": []}

    combined = "\n\n".join(all_raw_texts)

    # ── LLM synthesis ──
    # With Tavily: the AI answer + raw_content from search is already high quality.
    # Just do one synthesis pass instead of calling get_search_context separately.
    synth_input = combined[:8000]

    from langchain_core.messages import HumanMessage as _HM

    synth = await llm.ainvoke([_HM(content=(
        "Extract ALL specific data points from the provided content. "
        "List every number, score, stat, fact, date, name. Be exhaustive.\n\n"
        f"{synth_input}"
    ))])
    synthesis = synth.content if hasattr(synth, "content") else str(synth)

    # ── Structured data extraction for charts ──
    extract = await llm.ainvoke([_HM(content=f"""Extract chart data. Output JSON only.

Chart type rules:
- "bar": comparing categories (teams, scores) - "pie": proportions/percentages
- "line": trend over time - "multi_line": multiple trends
- "horizontal_bar": long labels

Return:
{{"viable": true, "chart_type": "bar", "title": "...", "x_label": "...", "y_label": "...",
  "labels": ["A","B"], "datasets": [{{"label": "S1", "values": [1,2]}}]}}

ONLY real numbers from text. None → {{"viable": false}}.

Text:
{combined[:4000]}""")])
    extract_text = extract.content if hasattr(extract, "content") else str(extract)

    structured = None
    m = re.search(r"\{.*\}", extract_text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            if d.get("viable") and d.get("labels") and d.get("datasets"):
                structured = d
        except Exception:
            pass

    return {"synthesis": synthesis, "structured_data": structured, "sources": all_sources}


def _has_search_provider() -> bool:
    """Return True if a ``type: search`` provider with an API key is configured."""
    p = get_provider_by_type("search")
    return p is not None and bool(p.api_key)
