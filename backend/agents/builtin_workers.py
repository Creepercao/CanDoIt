"""Built-in agent worker functions — extracted from graph.py.

Each worker filters its own tasks from the agent state, processes them,
and returns results under the appropriate state key.

Exports ``WORKER_MAP`` mapping agent-type names to their async worker functions.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import html as html_lib
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


def _json_from_text(text: str) -> dict | list | None:
    match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def _first_html_fragment(text: str) -> str:
    fence = re.search(r"```(?:html)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text.strip()


def _extract_ppt_plan(state: dict) -> dict:
    for item in state.get("skill_outputs", {}).get("ppt_planner", []):
        if isinstance(item, dict) and item.get("slides"):
            return item
    return {}


def _infer_requested_slide_count(text: str) -> int | None:
    """Infer explicit requested slide count from Chinese/English prompts."""
    patterns = [
        r"(\d{1,2})\s*(?:页|頁|张|張|slides?|pages?)",
        r"(?:页数|頁數|做成|制作成|生成|make|create)\D{0,12}(\d{1,2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 20:
                return value
    return None


def _normalize_ppt_slides(data: dict, requested_count: int | None, fallback_prompt: str) -> list[dict]:
    slides = data.get("slides") if isinstance(data.get("slides"), list) else []
    normalized = []
    max_count = requested_count or min(max(len(slides), 5), 8)

    for i, slide in enumerate(slides[:max_count], start=1):
        if not isinstance(slide, dict):
            continue
        slide["index"] = int(slide.get("index") or i)
        slide.setdefault("title", f"Slide {slide['index']}")
        slide.setdefault("goal", "")
        slide.setdefault("bullets", [])
        slide.setdefault("visual", "")
        normalized.append(slide)

    while len(normalized) < max_count:
        idx = len(normalized) + 1
        normalized.append({
            "index": idx,
            "title": f"补充页 {idx}",
            "goal": "补足用户要求的页数，并承接前后页面逻辑",
            "bullets": [fallback_prompt[:180] or "围绕主题展开关键内容"],
            "visual": "信息卡片与流程/关系图",
            "speaker_note": "",
        })

    for i, slide in enumerate(normalized, start=1):
        slide["index"] = i
    return normalized


def _deck_theme_css(theme: str) -> str:
    if theme == "warm-paper":
        return """
        body { background:#efe6d3; color:#262018; font-family: Georgia, 'Microsoft YaHei', serif; }
        .slide { background: radial-gradient(circle at 20% 10%, #fff7e6, #ead8b8 70%); color:#272017; }
        .kicker,.page-no { color:#8f5d2a; }
        .visual { border-color:#9c6b31; background:rgba(255,255,255,.38); }
        """
    if theme == "clean-white":
        return """
        body { background:#f5f7fb; color:#172033; font-family: Inter, 'Microsoft YaHei', sans-serif; }
        .slide { background: linear-gradient(135deg,#ffffff,#edf4ff); color:#172033; }
        .kicker,.page-no { color:#2563eb; }
        .visual { border-color:#93c5fd; background:rgba(37,99,235,.08); }
        """
    return """
    body { background:#070a12; color:#edf3ff; font-family: Inter, 'Microsoft YaHei', sans-serif; }
    .slide { background:
      radial-gradient(circle at 18% 12%, rgba(72,116,255,.38), transparent 28%),
      radial-gradient(circle at 85% 80%, rgba(255,122,48,.24), transparent 28%),
      linear-gradient(135deg,#090d1a,#111827 58%,#190d2f); color:#edf3ff; }
    .kicker,.page-no { color:#7dd3fc; }
    .visual { border-color:rgba(125,211,252,.55); background:rgba(15,23,42,.72); }
    """


def _fallback_slide_html(slide: dict, total: int) -> str:
    idx = int(slide.get("index", 1))
    title = html_lib.escape(str(slide.get("title", f"Slide {idx}")))
    goal = html_lib.escape(str(slide.get("goal", "")))
    bullets = slide.get("bullets") or []
    bullet_html = "\n".join(f"<li>{html_lib.escape(str(b))}</li>" for b in bullets[:5])
    visual = html_lib.escape(str(slide.get("visual", "核心关系图")))
    return f"""
    <section class="slide" data-slide="{idx}">
      <div class="kicker">PART {idx:02d}</div>
      <h1>{title}</h1>
      <p class="lead">{goal}</p>
      <div class="grid">
        <ul>{bullet_html}</ul>
        <div class="visual">{visual}</div>
      </div>
      <div class="page-no">{idx}/{total}</div>
    </section>
    """.strip()


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
            # Prefer configured MCP search/browser tools. If none are available,
            # or they fail, keep the native web-search path as fallback.
            mcp_hits = []
            try:
                from backend.mcp_adapter import mcp_manager, _build_tool_arguments, _result_text
                for spec in mcp_manager.preferred_research_tools()[:2]:
                    arguments = await _build_tool_arguments(state, spec, prompt_text)
                    raw = await mcp_manager.call_tool(spec.skill_name, arguments)
                    text = _result_text(raw)
                    if text:
                        mcp_hits.append({
                            "server": spec.server.name,
                            "tool": spec.name,
                            "text": text[:5000],
                            "raw": raw,
                        })
            except Exception as mcp_err:
                logger.debug(f"MCP research tools skipped: {mcp_err}")

            if mcp_hits:
                combined = "\n\n---\n\n".join(
                    f"[MCP:{hit['server']}/{hit['tool']}]\n{hit['text']}"
                    for hit in mcp_hits
                )
                synth = await llm.ainvoke([HumanMessage(
                    content=RESEARCH_SYNTHESIS_PROMPT.format(text=combined[:8000]))])
                synthesis = synth.content if hasattr(synth, "content") else str(synth)
                results.append({
                    "task": prompt_text,
                    "sources": [
                        {"title": f"MCP {hit['server']}/{hit['tool']}", "url": "", "snippet": hit["text"][:300]}
                        for hit in mcp_hits
                    ],
                    "synthesis": synthesis,
                    "raw_text": combined[:4000],
                    "structured_data": None,
                    "_from_mcp": True,
                })
                continue

            # ── A: Check semantic cache before searching ──
            from backend.research_cache import get_research_cache, get_knowledge_base
            rc = get_research_cache()
            # Try task prompt first, then user request (more stable across runs)
            user_req = state.get("user_request", "")
            cached = await rc.get_similar(prompt_text)
            if not cached and user_req and user_req != prompt_text:
                cached = await rc.get_similar(user_req)
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
                # Also store under the alternate key for future hits
                await rc.store(prompt_text, cached)
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


# ── Parallel PPT Workers ──

async def ppt_planner_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "ppt_planner"]
    if not my_tasks:
        return {"skill_outputs": {}}

    task = my_tasks[0]
    prompt_text = task.get("prompt") or state.get("user_request", "")
    requested_count = _infer_requested_slide_count(state.get("user_request", "") + "\n" + prompt_text)
    research_data = state.get("research_results", [])
    research_text = "\n\n".join(
        (r.get("synthesis", "") + "\n" + r.get("raw_text", "")[:1200]).strip()
        for r in research_data if isinstance(r, dict)
    )[:9000]

    llm = _get_chat_llm(state)
    planner_prompt = f"""你是演示文稿策划专家。基于用户请求和研究资料，规划一套 PPT/HTML 演示。

用户请求:
{state.get("user_request", prompt_text)}

研究资料:
{research_text or "(无研究资料，按用户请求规划)"}

只输出 JSON，不要 Markdown。格式:
{{
  "title": "演示标题",
  "theme": "dark-tech | warm-paper | clean-white",
  "slides": [
    {{
      "index": 1,
      "title": "页标题",
      "goal": "这一页要完成的表达目标",
      "bullets": ["要点1", "要点2", "要点3"],
      "visual": "建议的图形/布局/图示",
      "speaker_note": "这一页讲述提示"
    }}
  ]
}}

要求:
- 页数 5-8 页，除非用户明确指定
- 每页只负责一个明确观点
- 尽量把研究资料中的事实、数字、来源名称分配到具体页面
- visual 要具体，方便后续页面并行生成
"""
    try:
        resp = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=planner_prompt)]),
            timeout=180,
        )
        content = resp.content if hasattr(resp, "content") else str(resp)
        data = _json_from_text(content)
        if not isinstance(data, dict):
            raise ValueError("planner returned non-JSON")
    except Exception as e:
        logger.warning(f"PPT planner fallback: {e}")
        data = {
            "title": state.get("user_request", "演示文稿")[:60],
            "theme": "dark-tech",
            "slides": [
                {"index": 1, "title": "主题概览", "goal": "说明演示主题和背景", "bullets": [prompt_text], "visual": "标题页与核心关键词"},
                {"index": 2, "title": "关键信息", "goal": "总结研究得到的核心事实", "bullets": [research_text[:300] or prompt_text], "visual": "信息卡片组"},
                {"index": 3, "title": "结构拆解", "goal": "拆解主要逻辑", "bullets": ["背景", "机制", "影响"], "visual": "流程图"},
                {"index": 4, "title": "重点洞察", "goal": "提炼可行动结论", "bullets": ["洞察一", "洞察二", "洞察三"], "visual": "对比图"},
                {"index": 5, "title": "总结", "goal": "收束观点并给出下一步", "bullets": ["总结", "建议", "行动"], "visual": "结论页"},
            ],
        }

    from backend.tools.ppt_run_store import new_deck_id, save_plan

    data["slides"] = _normalize_ppt_slides(data, requested_count, prompt_text)
    data["theme"] = data.get("theme") or "dark-tech"
    data["deck_id"] = task.get("deck_id") or data.get("deck_id") or new_deck_id()
    data["requested_slide_count"] = requested_count
    save_plan(data["deck_id"], data)

    slide_tasks = [
        {
            "agent": "ppt_slide",
            "prompt": f"生成第 {slide['index']} 页: {slide.get('title', '')}",
            "slide_index": slide["index"],
            "slide_spec": slide,
            "deck_id": data["deck_id"],
            "task_group": "ppt_slides",
        }
        for slide in data["slides"]
    ]
    slide_tasks.append({
        "agent": "ppt_assembler",
        "prompt": "合并所有并行生成的 PPT 页面 HTML，输出完整翻页演示。",
        "deck_id": data["deck_id"],
        "task_group": "ppt_assemble",
    })

    return {
        "skill_outputs": {"ppt_planner": [data]},
        "_add_tasks": slide_tasks,
    }


async def ppt_slide_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "ppt_slide"]
    if not my_tasks:
        return {"skill_outputs": {}}

    plan = _extract_ppt_plan(state)
    all_slides = plan.get("slides", [])
    total = len(all_slides) or len(my_tasks)
    theme = plan.get("theme", "dark-tech")
    llm = _get_chat_llm(state)
    results = []

    for task in my_tasks:
        deck_id = task.get("deck_id") or plan.get("deck_id") or ""
        slide = task.get("slide_spec") or {}
        if not slide:
            idx = int(task.get("slide_index") or 1)
            slide = next((s for s in all_slides if int(s.get("index", 0)) == idx), {})
        idx = int(slide.get("index") or task.get("slide_index") or 1)

        prompt = f"""你是单页 PPT HTML 设计师。只生成一个 <section class="slide">...</section> 片段，不要完整 html/head/body。

整套演示标题: {plan.get("title", state.get("user_request", ""))}
主题风格: {theme}
总页数: {total}
当前页 JSON:
{json.dumps(slide, ensure_ascii=False, indent=2)}

硬性要求:
- 根元素必须是 <section class="slide" data-slide="{idx}">
- 必须包含 h1 标题、核心要点、一个视觉化区域
- 可以使用内联 SVG/CSS class，但不要输出 <script>
- 不要引入外部资源
- 控制在 80-180 行以内
"""
        status = "ok"
        error = ""
        try:
            resp = await asyncio.wait_for(
                llm.ainvoke([HumanMessage(content=prompt)]),
                timeout=150,
            )
            html = _first_html_fragment(resp.content if hasattr(resp, "content") else str(resp))
            if "<section" not in html:
                raise ValueError("missing section")
        except Exception as e:
            logger.warning(f"PPT slide {idx} fallback: {e}")
            status = "fallback"
            error = str(e)
            html = _fallback_slide_html(slide, total)

        if deck_id:
            from backend.tools.ppt_run_store import save_slide
            save_slide(
                deck_id,
                idx,
                html=html,
                title=slide.get("title", f"Slide {idx}"),
                status=status,
                error=error,
            )

        results.append({
            "task": task.get("prompt", ""),
            "deck_id": deck_id,
            "slide_index": idx,
            "html": html,
            "title": slide.get("title", f"Slide {idx}"),
            "status": status,
            "error": error,
        })

    return {"skill_outputs": {"ppt_slide": results}}


async def ppt_assembler_worker(state: dict) -> dict:
    my_tasks = [t for t in state.get("tasks", []) if t.get("agent") == "ppt_assembler"]
    if not my_tasks:
        return {"skill_outputs": {}}

    plan = _extract_ppt_plan(state)
    deck_id = my_tasks[0].get("deck_id") or plan.get("deck_id") or ""
    title = plan.get("title") or state.get("user_request", "PPT 演示")
    theme = plan.get("theme", "dark-tech")
    slide_items = state.get("skill_outputs", {}).get("ppt_slide", [])
    by_index: dict[int, dict] = {
        int(s.get("slide_index", 0)): s
        for s in slide_items
        if isinstance(s, dict) and s.get("html") and int(s.get("slide_index", 0) or 0) > 0
    }

    if deck_id:
        from backend.tools.ppt_run_store import load_slide_html, read_manifest
        manifest = read_manifest(deck_id)
        for key, meta in (manifest.get("slides") or {}).items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            if idx in by_index:
                continue
            stored_html = load_slide_html(deck_id, idx)
            if stored_html:
                by_index[idx] = {
                    "deck_id": deck_id,
                    "slide_index": idx,
                    "html": stored_html,
                    "title": meta.get("title", f"Slide {idx}"),
                    "status": meta.get("status", "stored"),
                    "error": meta.get("error", ""),
                }

    expected_slides = [
        int(slide.get("index") or idx + 1)
        for idx, slide in enumerate(plan.get("slides", []))
        if isinstance(slide, dict)
    ] or sorted(by_index.keys())
    total = len(expected_slides)
    failed_slides: list[dict] = []
    complete_items: list[dict] = []

    for slide_idx in expected_slides:
        item = by_index.get(slide_idx)
        slide_spec = next(
            (s for s in plan.get("slides", []) if int(s.get("index", 0) or 0) == slide_idx),
            {"index": slide_idx, "title": f"Slide {slide_idx}", "bullets": [], "visual": ""},
        )
        if not item:
            placeholder = _fallback_slide_html(slide_spec, total)
            item = {
                "deck_id": deck_id,
                "slide_index": slide_idx,
                "html": placeholder,
                "title": slide_spec.get("title", f"Slide {slide_idx}"),
                "status": "missing-placeholder",
                "error": "slide was not generated in this run",
            }
            if deck_id:
                from backend.tools.ppt_run_store import save_slide
                save_slide(
                    deck_id,
                    slide_idx,
                    html=placeholder,
                    title=item["title"],
                    status=item["status"],
                    error=item["error"],
                )
        if item.get("status") not in ("ok", "stored"):
            failed_slides.append({
                "slide_index": slide_idx,
                "status": item.get("status", ""),
                "error": item.get("error", ""),
                "title": item.get("title", f"Slide {slide_idx}"),
            })
        complete_items.append(item)

    sections = "\n\n".join(item["html"] for item in complete_items)
    css = _deck_theme_css(theme)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{html_lib.escape(str(title))}</title>
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin:0; width:100%; height:100%; overflow:hidden; }}
{css}
.deck {{ width:100vw; height:100vh; position:relative; overflow:hidden; }}
.slide {{
  display:none; width:100vw; height:100vh; padding:5.8vh 6.5vw; position:absolute; inset:0;
}}
.slide.active {{ display:block; animation: slideIn .5s ease both; }}
.kicker {{ font-size:1.1vw; letter-spacing:.12em; text-transform:uppercase; margin-bottom:1.2vh; }}
h1 {{ font-size:4vw; line-height:1.05; margin:0 0 2vh; max-width:78vw; }}
.lead {{ font-size:1.55vw; line-height:1.55; max-width:72vw; opacity:.88; }}
.grid {{ display:grid; grid-template-columns:1.05fr .95fr; gap:4vw; align-items:center; margin-top:4vh; }}
ul {{ margin:0; padding-left:1.3em; font-size:1.35vw; line-height:1.8; }}
.visual {{ min-height:34vh; border:1px solid; border-radius:22px; display:flex; align-items:center; justify-content:center; padding:2vw; font-size:1.55vw; text-align:center; box-shadow:0 24px 80px rgba(0,0,0,.28); }}
.page-no {{ position:absolute; right:4vw; bottom:3vh; font-size:1vw; opacity:.75; }}
.progress {{ position:fixed; left:0; top:0; height:4px; width:100%; background:rgba(255,255,255,.12); z-index:10; }}
.progress > span {{ display:block; height:100%; width:0; background:linear-gradient(90deg,#38bdf8,#f97316); transition:width .25s ease; }}
@keyframes slideIn {{ from {{ opacity:0; transform:translateY(18px); }} to {{ opacity:1; transform:none; }} }}
</style>
</head>
<body>
<div class="progress"><span id="bar"></span></div>
<main class="deck">
{sections}
</main>
<script>
const slides = Array.from(document.querySelectorAll('.slide'));
let current = 0;
function show(i) {{
  current = Math.max(0, Math.min(i, slides.length - 1));
  slides.forEach((s, idx) => s.classList.toggle('active', idx === current));
  const bar = document.getElementById('bar');
  if (bar) bar.style.width = (((current + 1) / Math.max(slides.length, 1)) * 100) + '%';
}}
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowRight' || e.key === ' ') show(current + 1);
  if (e.key === 'ArrowLeft') show(current - 1);
}});
document.addEventListener('click', (e) => show(current + (e.clientX > innerWidth / 2 ? 1 : -1)));
show(0);
</script>
</body>
</html>"""

    from backend.api import save_skill_html
    saved = save_skill_html("ppt-animation", html, title=str(title))
    if deck_id:
        from backend.tools.ppt_run_store import save_artifact
        save_artifact(deck_id, "assembled_html", {
            "html_url": saved["html_url"],
            "html_path": saved["file_path"],
            "title": saved["title"],
            "slides": total,
            "failed_slides": failed_slides,
        })

    return {"skill_outputs": {"ppt-animation": [{
        "task": my_tasks[0].get("prompt", ""),
        "result": html,
        "deck_id": deck_id,
        "html_url": saved["html_url"],
        "html_title": saved["title"],
        "html_path": saved["file_path"],
        "slides": total,
        "failed_slides": failed_slides,
        "source": "parallel-ppt",
    }]}}


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
    "ppt_planner": ppt_planner_worker,
    "ppt_slide": ppt_slide_worker,
    "ppt_assembler": ppt_assembler_worker,
}
"""Maps built-in agent type names to their worker functions."""
