"""Web search tool — Bing China (cn.bing.com, works inside GFW)."""
import re
import httpx
import logging
from urllib.parse import unquote

logger = logging.getLogger("search")


async def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
            results = []

            # Bing result blocks: <li class="b_algo"> each contains h2 > a
            blocks = re.findall(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', text, re.DOTALL)

            for block in blocks[:max_results]:
                # Title + URL: <h2><a href="url">title</a></h2>
                link = re.search(r'<a[^>]*href\s*=\s*"([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                if not link:
                    continue

                url = link.group(1)
                title = _clean(link.group(2))

                if not title or len(title) < 3:
                    continue

                # Snippet: <p> or <div class="b_caption">
                snippet = ""
                sn = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
                if sn:
                    snippet = _clean(sn.group(1))
                if not snippet:
                    sn = re.search(r'class="b_caption"[^>]*>(.*?)</div>', block, re.DOTALL)
                    if sn:
                        snippet = _clean(sn.group(1))

                results.append({"title": title, "url": url, "snippet": snippet})

            if results:
                return results

            # Fallback: broader parsing
            all_links = re.findall(r'<h2[^>]*>.*?<a[^>]*href\s*=\s*"(https?://[^"]+)"[^>]*>(.*?)</a>', text, re.DOTALL)
            for url, title in all_links[:max_results]:
                t = _clean(title)
                if t and len(t) > 3 and not any(s in url for s in ("bing.com", "microsoft.com", "go.microsoft.com")):
                    results.append({"title": t, "url": url, "snippet": ""})

            return results[:max_results]

    except Exception as e:
        logger.warning(f"Bing error: {e}")
        return _fallback()


def _fallback() -> list[dict[str, str]]:
    return [{"title": "Search unavailable", "snippet": "Bing search failed.", "url": ""}]


def _clean(text: str) -> str:
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&[a-z]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()
