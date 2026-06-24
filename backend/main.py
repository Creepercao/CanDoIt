"""FastAPI server — main entry point."""
import json
import os
import re
import uuid
import logging
from typing import AsyncGenerator

# Raise Starlette's multipart file size limit (default 1 MB → 200 MB)
from starlette.formparsers import MultiPartParser
MultiPartParser.max_file_size = 200 * 1024 * 1024  # 200 MB

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

from backend.config import PROVIDERS
from backend.models.registry import registry
from backend.models.provider import create_chat_model, get_default_chat_model
from backend.agents.graph import get_multi_agent_graph, rebuild_graph
from backend.tools.image_gen import generate_image
from backend.tools.video_gen import generate_video
from backend.skills.registry import skill_registry
from backend.skills.package_installer import (
    install_from_zip, uninstall_package, restore_from_disk,
    save_package_state, get_readme,
)
from backend.prompts import SYNTHESIZER_PROMPT_TEMPLATE

app = FastAPI(title="Multi-Agent Platform", version="1.0.0")

OUTPUTS_DIR = Path(__file__).parent.parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)
SKILL_HTML_DIR = OUTPUTS_DIR / "skills"
SKILL_HTML_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")

# Regex to detect HTML content in skill results
_HTML_DETECT_RE = re.compile(r'<!DOCTYPE\s+html|<html[\s>]', re.IGNORECASE)


def _save_skill_html(skill_name: str, content: str, title: str = "") -> dict:
    """Save HTML content from a skill worker to the outputs directory.

    Returns a dict with ``html_url``, ``file_path``, and ``title`` that
    replaces the raw HTML in the accumulated state (keeps state small).
    """
    file_id = uuid.uuid4().hex[:12]
    safe_name = re.sub(r'[^\w\-]', '_', skill_name)
    filename = f"{safe_name}_{file_id}.html"
    filepath = SKILL_HTML_DIR / filename

    # Extract title from HTML if not provided
    if not title:
        title_match = re.search(r'<title>(.*?)</title>', content, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

    filepath.write_text(content, encoding="utf-8")
    html_url = f"/outputs/skills/{filename}"

    logger.info(f"Saved skill HTML: {skill_name} → {filepath}")

    return {
        "html_url": html_url,
        "file_path": str(filepath),
        "title": title or f"{skill_name} output",
        "source_skill": skill_name,
    }

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ── Models ──

class ChatRequest(BaseModel):
    message: str
    chat_model_id: str = ""
    image_model_id: str = ""
    video_model_id: str = ""
    router_model_id: str = ""
    stream: bool = True

class ImageGenRequest(BaseModel):
    prompt: str; model_id: str = ""; negative_prompt: str = ""
    width: int = 1024; height: int = 1024; steps: int = 20

class VideoGenRequest(BaseModel):
    prompt: str; model_id: str = ""; duration: int = 5


# ── Labels ──

def _get_agent_labels() -> dict[str, str]:
    """Merge built-in and skill agent labels for SSE / frontend display."""
    base = {
        "supervisor": "🧠 主管", "research_worker": "🔍 研究员",
        "analyst_worker": "🔢 分析师", "chart_worker": "📊 图表师",
        "image_worker": "🎨 画师", "video_worker": "🎬 视频师",
        "code_worker": "💻 程序员", "synthesizer": "📝 整合",
    }
    base.update(skill_registry.get_agent_labels())
    return base


def _get_agent_map() -> dict[str, str]:
    """Merge built-in and skill agent→node mappings."""
    base = {
        "research": "research_worker", "analyst": "analyst_worker",
        "chart": "chart_worker", "image_gen": "image_worker",
        "video_gen": "video_worker", "code": "code_worker",
    }
    base.update(skill_registry.get_node_for_agent())
    return base


def _get_worker_nodes() -> set[str]:
    """All worker node names (built-in + skill)."""
    nodes = {
        "research_worker", "analyst_worker", "chart_worker",
        "image_worker", "video_worker", "code_worker",
    }
    nodes.update(skill_registry.get_node_funcs().keys())
    return nodes


def _get_result_keys() -> tuple[str, ...]:
    """All state keys that accumulate worker output."""
    return (
        "research_results", "analyst_results", "chart_results",
        "image_results", "video_results", "code_results",
        "skill_outputs",
    )


# ── Startup ──

@app.on_event("startup")
async def startup():
    await registry.refresh()
    skill_registry.discover()
    restore_from_disk(skill_registry)  # Restore persisted package skills
    get_multi_agent_graph()  # Build graph after skill discovery
    redis_status = "enabled" if os.environ.get("REDIS_URL") else "disabled"
    logger.info(
        f"Loaded {len(registry.get_all())} models | "
        f"{len(skill_registry.get_enabled())} skills | "
        f"Redis: {redis_status}"
    )


# ── API Routes ──

@app.get("/api/health")
async def health():
    return {"status": "ok", "models": len(registry.get_all()),
            "providers": len(PROVIDERS),
            "skills": len(skill_registry.get_enabled()),
            "redis": "enabled" if os.environ.get("REDIS_URL") else "disabled"}

@app.get("/api/providers")
async def list_providers():
    return [{"name": p.name, "base_url": p.base_url,
             "api_key_masked": p.apikey[:8] + "***" + p.apikey[-4:]} for p in PROVIDERS]

@app.get("/api/models")
async def list_models(type: str = ""):
    if not registry._fetched: await registry.refresh()
    models = registry.get_all() if not type else registry.get_by_type(type)
    return [{"id": m.id, "provider": m.provider, "type": m.type} for m in models]

@app.get("/api/models/chat")
async def list_chat_models():
    return [{"id": m.id, "provider": m.provider} for m in registry.get_chat_models()]

@app.get("/api/models/image")
async def list_image_models():
    return [{"id": m.id, "provider": m.provider} for m in registry.get_image_models()]

@app.get("/api/models/video")
async def list_video_models():
    return [{"id": m.id, "provider": m.provider} for m in registry.get_video_models()]

@app.post("/api/refresh-models")
async def refresh_models():
    await registry.refresh()
    return {"total": len(registry.get_all()), "chat": len(registry.get_chat_models()),
            "image": len(registry.get_image_models()), "video": len(registry.get_video_models())}

@app.post("/api/chat")
async def chat(req: ChatRequest):
    logger.info(f"CHAT REQ: model={req.chat_model_id[:30]} msg={req.message[:100]}")
    if req.stream:
        return StreamingResponse(
            _stream_chat(req), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _run_agents(req)

@app.post("/api/chat/sync")
async def chat_sync(req: ChatRequest):
    return await _run_agents(req)

@app.post("/api/generate-image")
async def api_generate_image(req: ImageGenRequest):
    return await generate_image(
        prompt=req.prompt, model_id=req.model_id or "stabilityai/stable-diffusion-3-5-large",
        negative_prompt=req.negative_prompt, width=req.width, height=req.height, steps=req.steps)

@app.post("/api/generate-video")
async def api_generate_video(req: VideoGenRequest):
    return await generate_video(prompt=req.prompt, model_id=req.model_id, duration=req.duration)


# ── Skill API ──

@app.get("/api/skills")
async def list_skills():
    """Return all registered skills with metadata."""
    return {
        "skills": skill_registry.to_api_list(),
        "count": len(skill_registry.get_all()),
        "enabled_count": len(skill_registry.get_enabled()),
    }


@app.get("/api/skills/{name}")
async def get_skill_detail(name: str):
    """Get detailed info about a specific skill."""
    skill = skill_registry.get(name)
    if not skill:
        return {"error": f"Skill '{name}' not found"}
    matches = [s for s in skill_registry.to_api_list() if s["name"] == name]
    return matches[0] if matches else {"error": f"Skill '{name}' not found"}


@app.post("/api/skills/{name}/toggle")
async def toggle_skill(name: str, enabled: bool = True):
    """Enable or disable a skill. Triggers graph rebuild."""
    skill = skill_registry.get(name)
    if not skill:
        return {"error": f"Skill '{name}' not found"}
    skill.enabled = enabled
    rebuild_graph()
    # Persist for package skills
    if skill._package_meta and skill._package_meta.get("source") == "package":
        save_package_state(name, enabled)
    logger.info(f"Skill '{name}' {'enabled' if enabled else 'disabled'} — graph rebuilt")
    return {"name": name, "enabled": enabled, "graph_rebuilt": True}


# ── Skill Package Management ──

@app.post("/api/skills/packages/install")
async def install_skill_package(file: UploadFile = File(...)):
    """Upload a .zip file containing standard skill packages (SKILL.md format).

    Extracts, registers, and persists each skill found in the archive.
    Rebuilds the agent graph on success.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        return {"error": "Only .zip files are accepted"}

    try:
        zip_bytes = await file.read()
    except Exception as e:
        return {"error": f"Failed to read uploaded file: {e}"}

    if len(zip_bytes) > 200 * 1024 * 1024:
        return {"error": "File too large (max 200 MB)"}

    result = install_from_zip(zip_bytes, skill_registry)

    if result["installed"]:
        rebuild_graph()

    return result


@app.delete("/api/skills/packages/{name}")
async def uninstall_skill_package(name: str):
    """Uninstall a standard skill package. Removes files, unregisters, rebuilds graph."""
    skill = skill_registry.get(name)
    if not skill:
        return {"error": f"Skill '{name}' not found"}

    if not skill._package_meta or skill._package_meta.get("source") != "package":
        return {"error": f"'{name}' is not a package skill and cannot be uninstalled"}

    ok = uninstall_package(name, skill_registry)
    if ok:
        rebuild_graph()
        return {"name": name, "uninstalled": True, "graph_rebuilt": True}
    return {"error": f"Failed to uninstall '{name}'"}


@app.get("/api/skills/packages")
async def list_packages():
    """List installed standard skill packages."""
    from backend.skills.package_installer import _load_packages_json
    packages = _load_packages_json()
    return {"packages": packages, "count": len(packages)}


@app.get("/api/skills/{name}/readme")
async def get_skill_readme(name: str):
    """Get the full SKILL.md content for a skill."""
    content = skill_registry.get_readme(name)
    if content is None:
        return {"error": f"No readme found for skill '{name}'"}
    return {"name": name, "content": content}


# ── PPTX Export ─────────────────────────────────────────────────────

class PPTXExportRequest(BaseModel):
    html_content: str = ""
    html_url: str = ""
    title: str = ""


@app.post("/api/skills/ppt-animation/export-pptx")
async def export_pptx(req: PPTXExportRequest):
    """Export HTML slide content as a .pptx PowerPoint file.

    Accepts either raw ``html_content`` or a ``html_url`` pointing to a
    previously saved skill output file. Returns the .pptx file as a download.
    """
    from backend.tools.ppt_export import export_skill_to_pptx

    html = req.html_content

    # If html_url is provided, read from the saved file
    if not html and req.html_url:
        # Resolve URL path to filesystem path
        url_path = req.html_url.replace("/outputs/", "", 1)
        file_path = OUTPUTS_DIR / url_path
        if file_path.exists():
            html = file_path.read_text(encoding="utf-8")
        else:
            return {"error": f"HTML file not found: {req.html_url}"}

    if not html:
        return {"error": "No HTML content provided (use html_content or html_url)"}

    if not re.search(r'<!DOCTYPE\s+html|<html[\s>]', html, re.IGNORECASE):
        return {"error": "Content does not appear to be HTML"}

    try:
        result = export_skill_to_pptx(html, title=req.title or "")
        return {
            "success": True,
            "file_url": result["file_url"],
            "slides": result["slides"],
            "title": result["title"],
        }
    except Exception as e:
        logger.error(f"PPTX export error: {e}")
        return {"error": f"Export failed: {e}"}


@app.get("/api/skills/ppt-animation/export-pptx/{filename}")
async def download_pptx(filename: str):
    """Download a previously exported PPTX file."""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    file_path = OUTPUTS_DIR / safe_name
    if not file_path.exists() or not safe_name.endswith(".pptx"):
        return {"error": "File not found"}
    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


# ── SSE Helper ──

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Sync Run ──

def _make_state(req: ChatRequest, skip_synthesizer: bool = False) -> dict:
    return {
        "messages": [], "user_request": req.message, "tasks": [],
        "image_results": [], "video_results": [], "research_results": [],
        "analyst_results": [], "code_results": [], "chart_results": [],
        "skill_outputs": {},
        "final_response": "",
        "chat_model_id": req.chat_model_id, "image_model_id": req.image_model_id,
        "video_model_id": req.video_model_id, "router_model_id": req.router_model_id,
        "_skip_synthesizer": skip_synthesizer,
    }

async def _run_agents(req: ChatRequest) -> dict:
    initial_state = _make_state(req)
    try:
        result = await get_multi_agent_graph().ainvoke(initial_state)
        return {
            "success": True,
            "response": result.get("final_response", ""),
            "tasks": result.get("tasks", []),
            "research_results": result.get("research_results", []),
            "analyst_results": result.get("analyst_results", []),
            "image_results": result.get("image_results", []),
            "video_results": result.get("video_results", []),
            "code_results": result.get("code_results", []),
            "chart_results": result.get("chart_results", []),
            "skill_outputs": result.get("skill_outputs", {}),
        }
    except Exception as e:
        return {"success": False, "response": f"Error: {str(e)}", "error": str(e)}


# ── Streaming Orchestrator (hybrid: LangGraph for agents + manual LLM streaming) ──

async def _stream_chat(req: ChatRequest) -> AsyncGenerator[str, None]:
    """Hybrid streaming: LangGraph handles agent dependency ordering,
    manual LLM astream provides reliable token-level SSE output.

    The graph's synthesizer_node is skipped (_skip_synthesizer=True).
    Worker results are accumulated from astream_events output.
    Chart/image/skill results included in final SSE for frontend rendering.

    Emits: phase, plan, agent_start, agent_done, token, final, done
    """
    initial_state = _make_state(req, skip_synthesizer=True)
    chat_id = req.chat_model_id or ""

    # ── Phase 1: Run graph (supervisor → workers, synthesizer is no-op) ──
    yield _sse("phase", {"phase": "supervisor", "message": "分析请求，规划任务..."})

    accumulated: dict[str, list] = {}
    emitted_agents: set = set()
    agent_labels = _get_agent_labels()
    worker_nodes = _get_worker_nodes()

    try:
        async for event in get_multi_agent_graph().astream_events(initial_state):
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            # Supervisor completed → check direct response / emit plan
            if kind == "on_chain_end" and name == "supervisor":
                output = _extract_output(data)
                tasks = output.get("tasks", [])

                if output.get("final_response") and not tasks:
                    yield _sse("phase", {"phase": "done", "message": "直接回复"})
                    yield _sse("final", {"response": output["final_response"]})
                    yield _sse("done", {})
                    return

                if not tasks:
                    yield _sse("final", {"response": "无法理解请求"})
                    yield _sse("done", {})
                    return

                yield _sse("plan", {
                    "tasks": [{"agent": t.get("agent", ""), "prompt": t.get("prompt", "")[:80]} for t in tasks],
                    "count": len(tasks),
                })

            # Worker started
            elif kind == "on_chain_start" and name in worker_nodes:
                if name not in emitted_agents:
                    emitted_agents.add(name)
                    yield _sse("agent_start", {
                        "agent": name,
                        "label": agent_labels.get(name, name),
                        "task": "",
                        "index": 0,
                    })

            # Worker completed — accumulate results for synthesizer prompt
            elif kind == "on_chain_end" and name in worker_nodes:
                yield _sse("agent_done", {
                    "agent": name,
                    "label": agent_labels.get(name, name),
                })
                output = _extract_output(data)
                for key in _get_result_keys():
                    if key in output and output[key]:
                        accumulated.setdefault(key, []).extend(
                            output[key] if isinstance(output[key], list) else [output[key]]
                        )
                        agent_label = agent_labels.get(name, name)
                        results_list = output[key]
                        if key == "research_results":
                            sources = sum(1 for r in results_list if isinstance(r, dict) and r.get("url"))
                            yield _sse("token", {"text": "\n\n> " + agent_label + ": found " + str(sources or len(results_list)) + " sources\n"})
                        elif key == "analyst_results":
                            yield _sse("token", {"text": "\n\n> " + agent_label + ": structured data extracted\n"})
                        elif key == "chart_results":
                            has_url = any((r.get("result", {}) or {}).get("url") for r in results_list)
                            if has_url:
                                yield _sse("token", {"text": "\n\n> " + agent_label + ": chart generated\n\n"})
                            else:
                                yield _sse("token", {"text": "\n\n> " + agent_label + ": done\n"})
                        elif key == "image_results":
                            yield _sse("token", {"text": "\n\n> " + agent_label + ": image generated\n\n"})
                        elif key == "skill_outputs":
                            # Flatten skill outputs — save HTML to files so frontend can display them
                            if isinstance(results_list, dict):
                                for skill_name, skill_items in results_list.items():
                                    skill = skill_registry.get(skill_name)
                                    label = f"{skill.emoji} {skill.display_name}" if skill else skill_name
                                    # Save HTML results to files for download/preview
                                    html_urls = []
                                    for item in (skill_items or []):
                                        if isinstance(item, dict):
                                            result_text = item.get("result", "")
                                            if isinstance(result_text, str) and _HTML_DETECT_RE.search(result_text[:500]):
                                                saved = _save_skill_html(skill_name, result_text,
                                                    title=item.get("task", ""))
                                                item["html_url"] = saved["html_url"]
                                                item["html_title"] = saved["title"]
                                                html_urls.append(saved["html_url"])
                                    if html_urls:
                                        url_links = ", ".join(f"[{u.split('/')[-1]}]({u})" for u in html_urls)
                                        yield _sse("token", {"text": f"\n\n> {label}: 已生成 ({len(skill_items)} 个结果)\n\n{url_links}\n"})
                                    else:
                                        yield _sse("token", {"text": f"\n\n> {label}: completed ({len(skill_items)} results)\n"})
                        else:
                            yield _sse("token", {"text": "\n\n> " + agent_label + ": done\n"})

    except Exception as e:
        logger.error(f"Graph streaming error: {e}")
        yield _sse("error", {"error": str(e)})
        yield _sse("done", {})
        return

    # ── Phase 2: Manual synthesizer streaming (guaranteed token-level output) ──
    yield _sse("phase", {"phase": "synthesize", "message": "整合结果..."})

    synth_prompt = _build_synth_prompt(req.message, accumulated)
    full_response = ""

    from langchain_core.messages import HumanMessage

    try:
        llm = create_chat_model(
            model_id=chat_id or "deepseek-ai/DeepSeek-V3",
            temperature=0.7, max_tokens=4096, provider_config=None,
        )
        async for chunk in llm.astream([HumanMessage(content=synth_prompt)]):
            text = chunk.content if hasattr(chunk, "content") else str(chunk)
            if text:
                full_response += text
                yield _sse("token", {"text": text})
    except Exception as e:
        logger.error(f"Synthesizer streaming error: {e}")
        yield _sse("error", {"error": f"Synthesis failed: {e}"})
        yield _sse("done", {})
        return

    # Include chart/image results so frontend can render them alongside markdown
    yield _sse("final", {
        "response": full_response,
        "chart_results": accumulated.get("chart_results", []),
        "image_results": accumulated.get("image_results", []),
    })
    yield _sse("done", {})


def _extract_output(data: dict) -> dict:
    """Robustly extract node output from an astream_events event data.

    In LangGraph + astream_events the output lives under data["output"]
    for on_chain_end events, but depending on the LangChain version it
    can also appear inside data["chunk"] (especially for streamed chains).
    """
    out = data.get("output")
    if isinstance(out, dict):
        return out
    chunk = data.get("chunk")
    if isinstance(chunk, dict):
        return chunk
    return {}


def _build_synth_prompt(user_request: str, state: dict) -> str:
    """Build synthesizer prompt from accumulated worker results.

    Includes chart URLs so the LLM can reference them with ![](url) syntax.
    The prompt explicitly instructs the LLM to include data tables verbatim.
    """
    research = json.dumps(state.get("research_results", []), ensure_ascii=False, indent=2)
    analyst = json.dumps(state.get("analyst_results", []), ensure_ascii=False, indent=2)
    charts = json.dumps(state.get("chart_results", []), ensure_ascii=False, indent=2)
    images = json.dumps(state.get("image_results", []), ensure_ascii=False, indent=2)
    videos = json.dumps(state.get("video_results", []), ensure_ascii=False, indent=2)
    codes = json.dumps(state.get("code_results", []), ensure_ascii=False, indent=2)
    skill_outputs_json = json.dumps(state.get("skill_outputs", {}), ensure_ascii=False, indent=2)

    # Build inline markdown tables from chart results
    table_blocks = ""
    for cr in state.get("chart_results", []):
        if cr.get("table_markdown"):
            title = cr.get("chart_spec", {}).get("title", "数据表")
            table_blocks += f"\n\n**{title}**\n\n{cr['table_markdown']}\n"

    return SYNTHESIZER_PROMPT_TEMPLATE.format(
        user_request=user_request,
        research=research if research != '[]' else 'None',
        analyst=analyst if analyst != '[]' else 'None',
        charts=charts if charts != '[]' else 'None',
        images=images if images != '[]' else 'None',
        videos=videos if videos != '[]' else 'None',
        codes=codes if codes != '[]' else 'None',
        skill_outputs=skill_outputs_json if skill_outputs_json != '{{}}' else 'None',
        table_blocks=table_blocks if table_blocks else "(no tables generated)",
    )


# ── Main ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
