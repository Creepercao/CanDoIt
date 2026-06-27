"""Built-in agent worker functions — extracted from graph.py.

Each worker filters its own tasks from the agent state, processes them,
and returns results under the appropriate state key.

Exports ``WORKER_MAP`` mapping agent-type names to their async worker functions.
"""

from __future__ import annotations

import json
import re
import logging
from typing import Any

from langchain_core.messages import HumanMessage

from backend.models.provider import create_chat_model
from backend.tools.image_gen import generate_image
from backend.tools.video_gen import generate_video
from backend.tools.chart_gen import generate_chart
from backend.tools.data_scraper import search_and_scrape
from backend.prompts import (
    RESEARCH_QUERY_PROMPT,
    RESEARCH_SYNTHESIS_PROMPT,
    ANALYST_PROMPT,
    IMAGE_ENHANCE_PROMPT,
    CODE_WORKER_PROMPT,
)

logger = logging.getLogger("graph.workers")


# ── Helpers ──

def _get_chat_llm(state: dict) -> Any:
    model_id = state.get("chat_model_id", "")
    return create_chat_model(model_id or "deepseek-ai/DeepSeek-V3", temperature=0.7, max_tokens=4096)


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) if data else "None"


def _build_table_md(spec: dict) -> str:
    labels = spec.get("labels", [])
    datasets = spec.get("datasets", [])
    if not labels or not datasets:
        return ""
    headers = ["类别"] + [ds.get("label", "值") for ds in datasets]
    header_line = "| " + " | ".join(headers) + " |"
    align_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    rows = []
    for i, label in enumerate(labels):
        row = [str(label)]
        for ds in datasets:
            vals = ds.get("values", [])
            row.append(str(vals[i]) if i < len(vals) else "-")
        rows.append("| " + " | ".join(row) + " |")
    return header_line + "\n" + align_line + "\n" + "\n".join(rows)


# ── Research Worker ──

async def research_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "research"]
    if not my_tasks:
        return {"research_results": []}

    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            # ── A: Check semantic cache before searching ──
            from backend.research_cache import get_research_cache, get_knowledge_base
            rc = get_research_cache()
            cached = await rc.get_similar(prompt_text)
            if cached:
                logger.info(f"Research cache HIT, skipping web search: {prompt_text[:60]}...")
                results.append({
                    "task": prompt_text,
                    "sources": cached.get("sources", []),
                    "synthesis": cached.get("synthesis", ""),
                    "raw_text": cached.get("raw_text", ""),
                    "structured_data": cached.get("structured_data"),
                    "_from_cache": True,
                })
                continue
            # ── End cache check ──

            all_sources = []
            all_synthesis = []
            seen_urls = set()

            # With Tavily (agent-optimised search), one call is usually enough.
            # Without Tavily, generate multiple queries for broader coverage.
            from backend.config import get_provider_by_type
            has_tavily = get_provider_by_type("search") is not None

            queries = [prompt_text]
            if not has_tavily and len(prompt_text) > 20:
                kw_resp = await llm.ainvoke([HumanMessage(
                    content=RESEARCH_QUERY_PROMPT.format(text=prompt_text))])
                extra = kw_resp.content if hasattr(kw_resp, "content") else str(kw_resp)
                for line in extra.strip().split("\n"):
                    q = line.strip().lstrip("0123456789.-) ").strip()
                    if q and len(q) > 5:
                        queries.append(q[:120])

            max_queries = 2 if has_tavily else 4  # Tavily returns richer results
            for query in queries[:max_queries]:
                scraped = await search_and_scrape(query, llm, max_pages=3)
                raw_text = scraped.get("synthesis", "")
                sources = scraped.get("sources", [])
                structured_data = scraped.get("structured_data")

                for src in sources:
                    url = src.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_sources.append(src)

                if raw_text and len(raw_text) > 50:
                    all_synthesis.append(raw_text)

            combined_text = "\n\n---\n\n".join(all_synthesis) if all_synthesis else ""
            if combined_text:
                synth = await llm.ainvoke([HumanMessage(
                    content=RESEARCH_SYNTHESIS_PROMPT.format(text=combined_text[:8000]))])
                synthesis = synth.content if hasattr(synth, "content") else str(synth)
            else:
                synthesis = "No data found from web search."

            result_entry = {
                "task": prompt_text, "sources": all_sources[:8],
                "synthesis": synthesis, "raw_text": combined_text[:4000],
                "structured_data": structured_data,
            }
            results.append(result_entry)

            # ── Store in cache + knowledge base ──
            try:
                await rc.store(prompt_text, result_entry)
                kb = get_knowledge_base()
                if kb:
                    await kb.add(prompt_text, result_entry)
            except Exception as cache_err:
                logger.debug(f"Cache store skipped: {cache_err}")
            # ── End cache store ──

        except Exception as e:
            logger.error(f"Research worker error: {e}")
            results.append({"task": prompt_text, "error": str(e), "synthesis": f"Search error: {e}"})

    return {"research_results": results}


# ── Analyst Worker ──

async def analyst_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "analyst"]
    if not my_tasks:
        return {"analyst_results": []}

    research_data = state.get("research_results", [])
    data_source = ""
    if research_data:
        data_source = "\n\n".join(
            r.get("synthesis", "") + "\n" + r.get("raw_text", "")[:2000]
            for r in research_data
        )

    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        source = data_source or prompt_text
        try:
            prompt = ANALYST_PROMPT.format(text=source[:6000])
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            content = resp.content if hasattr(resp, "content") else str(resp)

            structured = None
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                    if data.get("viable") and data.get("labels") and data.get("datasets"):
                        structured = data
                except json.JSONDecodeError:
                    pass

            results.append({
                "task": prompt_text,
                "structured_data": structured,
                "source_data": source[:500],
            })
        except Exception as e:
            logger.error(f"Analyst worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"analyst_results": results}


# ── Chart Worker ──

async def chart_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "chart"]
    if not my_tasks:
        return {"chart_results": []}

    chart_spec = None
    for r in state.get("analyst_results", []):
        if r.get("structured_data"):
            chart_spec = r["structured_data"]
            break
    if not chart_spec:
        for r in state.get("research_results", []):
            if r.get("structured_data"):
                chart_spec = r["structured_data"]
                break

    results = []
    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            if chart_spec:
                result = await generate_chart(
                    chart_type=chart_spec["chart_type"], title=chart_spec["title"],
                    labels=chart_spec["labels"], datasets=chart_spec["datasets"],
                    x_label=chart_spec.get("x_label", ""), y_label=chart_spec.get("y_label", ""),
                )
                table_md = _build_table_md(chart_spec)
                results.append({
                    "task": prompt_text, "chart_spec": chart_spec,
                    "result": result, "table_markdown": table_md,
                })
            else:
                results.append({
                    "task": prompt_text,
                    "result": {"error": "No structured data from analyst/research"},
                    "fallback": True,
                })
        except Exception as e:
            logger.error(f"Chart worker error: {e}")
            results.append({"task": prompt_text, "result": {"error": str(e)}})

    return {"chart_results": results}


# ── Image Worker ──

async def image_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "image_gen"]
    if not my_tasks:
        return {"image_results": []}

    image_model = state.get("image_model_id", "stabilityai/stable-diffusion-3-5-large")
    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            enhanced = await llm.ainvoke([HumanMessage(
                content=IMAGE_ENHANCE_PROMPT.format(prompt=prompt_text))])
            enhanced_text = enhanced.content if hasattr(enhanced, "content") else str(enhanced)
            result = await generate_image(prompt=enhanced_text.strip(), model_id=image_model)
            results.append({"task": prompt_text, "prompt_used": enhanced_text.strip(), "result": result})
        except Exception as e:
            logger.error(f"Image worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"image_results": results}


# ── Video Worker ──

async def video_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "video_gen"]
    if not my_tasks:
        return {"video_results": []}

    video_model = state.get("video_model_id", "")
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            result = await generate_video(prompt=prompt_text, model_id=video_model)
            if result.get("status") == "unsupported":
                img_result = await generate_image(
                    prompt=f"Key frame of video: {prompt_text}",
                    model_id=state.get("image_model_id", "stabilityai/stable-diffusion-3-5-large"))
                results.append({"task": prompt_text, "result": result, "fallback_image": img_result})
            else:
                results.append({"task": prompt_text, "result": result})
        except Exception as e:
            logger.error(f"Video worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"video_results": results}


# ── Code Worker ──

async def code_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "code"]
    if not my_tasks:
        return {"code_results": []}

    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        prompt_text = task.get("prompt", "")
        try:
            resp = await llm.ainvoke([HumanMessage(
                content=CODE_WORKER_PROMPT.format(prompt=prompt_text))])
            content = resp.content if hasattr(resp, "content") else str(resp)
            results.append({"task": prompt_text, "code": content})
        except Exception as e:
            logger.error(f"Code worker error: {e}")
            results.append({"task": prompt_text, "error": str(e)})

    return {"code_results": results}


# ── Synthesizer ──

async def synthesizer_node(state: dict) -> dict:
    """Synthesize all worker results into a markdown response.

    When _skip_synthesizer is True (streaming path), this is a no-op —
    the streaming orchestrator handles synthesis with token-level streaming.
    """
    if state.get("final_response") or state.get("_skip_synthesizer"):
        return {}

    llm = _get_chat_llm(state)

    table_blocks = ""
    for cr in state.get("chart_results", []):
        if cr.get("table_markdown"):
            title = cr.get("chart_spec", {}).get("title", "数据表")
            table_blocks += f"\n\n**{title}**\n\n{cr['table_markdown']}\n"

    # ── Extract HTML links from skill outputs ──
    html_links_section = ""
    for skill_name, items in state.get("skill_outputs", {}).items():
        for item in (items or []):
            if isinstance(item, dict) and item.get("html_url"):
                title = item.get("html_title", skill_name)
                html_links_section += (
                    f"\n- [{title}]({item['html_url']}) (generated by {skill_name})"
                )
    if html_links_section:
        html_links_section = (
            "\n\n## Generated HTML Files (must include as links)\n"
            + html_links_section
            + "\n"
        )

    prompt = f"""Synthesize agent results.

Original request: {state["user_request"]}

Research: {_fmt(state.get("research_results", []))}
Analyst data: {_fmt(state.get("analyst_results", []))}
Charts: {_fmt(state.get("chart_results", []))}
Images: {_fmt(state.get("image_results", []))}
Videos: {_fmt(state.get("video_results", []))}
Code: {_fmt(state.get("code_results", []))}
Skill outputs: {_fmt(state.get("skill_outputs", {}))}
{html_links_section}
Include these tables in response (copy verbatim):
{table_blocks}

Create comprehensive markdown response with data tables and chart descriptions.
If there are Generated HTML Files above, you MUST include them as [Title](url) markdown links."""

    full = ""
    try:
        async for chunk in llm.astream([HumanMessage(content=prompt)]):
            text = chunk.content if hasattr(chunk, "content") else str(chunk)
            if text:
                full += text
    except Exception as e:
        logger.error(f"Synthesizer stream error: {e}")

    if not full:
        # Fallback: return research data directly instead of empty response
        logger.warning("Synthesizer produced empty output — using raw research")
        research_text = ""
        for r in state.get("research_results", []):
            syn = r.get("synthesis", "")
            if syn and len(syn) > 50:
                research_text = syn[:2000]
                break
        return {"final_response": research_text or "No data available."}

    return {"final_response": full}


# ── Worker Map ──

WORKER_MAP: dict[str, callable] = {
    "research": research_worker,
    "analyst": analyst_worker,
    "chart": chart_worker,
    "image_gen": image_worker,
    "video_gen": video_worker,
    "code": code_worker,
}
"""Maps built-in agent type names to their worker functions."""
