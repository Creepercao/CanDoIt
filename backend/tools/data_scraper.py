"""Data scraper — trafilatura for robust text extraction, Bing search, multi-query."""
from __future__ import annotations

import re
import json
import logging
import httpx
from typing import Any

import trafilatura

from backend.tools.web_search import web_search

logger = logging.getLogger("scraper")

# Sports score sites accessible from China
SPORTS_SITES = [
    "sports.sina.com.cn", "sports.qq.com", "sports.163.com",
    "7m.com.cn", "zgzcw.com", "livescore.com", "bf.7m.com.cn",
    "live.zgzcw.com", "sports.cctv.com",
]
# Known blocked/captcha domains to skip scraping
SKIP_SCRAPE = ("sogou.com/link", "sogou.com/web", "baidu.com/link",
               "baike.baidu.com", "baidu.com/s", "zhidao.baidu.com",
               "bing.com", "microsoft.com", "go.microsoft.com")


async def scrape_page(url: str, max_len: int = 8000) -> str:
    """Fetch and extract clean text using trafilatura."""
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

        # trafilatura extract — handles JS-free pages, removes nav/ads/scripts
        text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                    no_fallback=False, favor_precision=True)
        if text and len(text) > 100:
            return text[:max_len]

        # Fallback: trafilatura with relaxed settings
        text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                    no_fallback=True)
        if text and len(text) > 50:
            return text[:max_len]

        return ""
    except Exception as e:
        logger.debug(f"Scrape failed for {url[:60]}: {e}")
        return ""


async def search_and_scrape(query: str, llm, max_pages: int = 3) -> dict:
    """Multi-query Bing search + trafilatura scraping + LLM data extraction."""

    # Generate optimized search queries
    queries = [query]
    # For sports: add English + site-filtered variants
    sports_kw = any(kw in query.lower() for kw in
        ["世界杯", "world cup", "比分", "score", "比赛", "match", "足球",
         "football", "soccer", "联赛", "league", "积分", "排名", "standings"])
    if sports_kw:
        queries.append(query + " scores results")
        for site in SPORTS_SITES[:3]:
            queries.append(f"site:{site} {query[:40]}")

    all_snippets = []
    all_sources = []
    scraped_texts = []

    for q in queries[:3]:
        results = await web_search(q, max_results=4)
        for r in results:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            url = r.get("url", "")

            if not title or "error" in title.lower() or "unavailable" in title.lower():
                continue

            all_snippets.append(f"[{title}]\n{snippet}\n{url}")
            all_sources.append({"title": title, "url": url})

            # Scrape accessible pages
            if url and not any(s in url for s in SKIP_SCRAPE):
                text = await scrape_page(url, max_len=6000)
                if text and len(text) > 200:
                    scraped_texts.append(f"--- {title} ---\n{text}")

    # Combine scraped pages first (richest), then snippets
    if scraped_texts:
        combined = "\n\n".join(scraped_texts)
    else:
        combined = "\n\n".join(all_snippets)

    if not combined.strip():
        return {"synthesis": "No data found.", "structured_data": None, "sources": []}

    # LLM synthesis
    from langchain_core.messages import HumanMessage as _HM
    synth = await llm.ainvoke([_HM(content=
        f"Extract ALL specific data points from these pages. List every number, score, stat, fact. Be exhaustive.\n\n{combined[:6000]}")])
    synthesis = synth.content if hasattr(synth, "content") else str(synth)

    return {"synthesis": synthesis, "structured_data": None, "sources": all_sources}
