"""FastAPI server — main entry point."""
import json
import re
import uuid
import logging
from typing import AsyncGenerator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api")

from backend.config import PROVIDERS
from backend.models.registry import registry
from backend.models.provider import create_chat_model
from backend.agents.graph import (
    get_multi_agent_graph, rebuild_graph,
    AGENT_LABELS, AGENT_MAP, WORKER_NODES, RESULT_KEYS,
)
from backend.tools.image_gen import generate_image
from backend.tools.video_gen import generate_video
from backend.skills.registry import skill_registry
from backend.skills.package_installer import restore_from_disk
from backend.prompts import SYNTHESIZER_PROMPT_TEMPLATE

app = FastAPI(title="Multi-Agent Platform", version="1.0.0")

OUTPUTS_DIR = Path(__file__).parent.parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)
SKILL_HTML_DIR = OUTPUTS_DIR / "skills"
SKILL_HTML_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")

_HTML_DETECT_RE = re.compile(r'<!DOCTYPE\s+html|<html[\s>]', re.IGNORECASE)


def _save_skill_html(skill_name: str, content: str, title: str = "") -> dict:
    """Save HTML content from a skill worker to the outputs directory."""
    file_id = uuid.uuid4().hex[:12]
    safe_name = re.sub(r'[^\w\-]', '_', skill_name)
    filename = f"{safe_name}_{file_id}.html"
    filepath = SKILL_HTML_DIR / filename

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


# ── Startup ──

@app.on_event("startup")
async def startup():
    await registry.refresh()
    skill_registry.discover()
    restore_from_disk(skill_registry)
    get_multi_agent_graph()
    logger.info(
        f"Loaded {len(registry.get_all())} models | "
        f"{len(skill_registry.get_enabled())} skills"
    )


# ── API Routes ──

@app.get("/api/health")
async def health():
    return {"status": "ok", "models": len(registry.get_all()),
            "providers": len(PROVIDERS),
            "skills": len(skill_registry.get_enabled())}

@app.get("/api/providers")
async def list_providers():
    return [{"name": p.name, "base_url": p.base_url,
             "api_key_masked": p.apikey[:8] + "***" + p.apikey[-4:]} for p in PROVIDERS]

@app.get("/api/models")
async def list_models(type: str = ""):
    if not registry._fetched: await registry.refresh()
    models = registry.list_models(type)
    return [{"id": m.id, "provider": m.provider, "type": m.type} for m in models]

@app.post("/api/chat")
async def chat(req: ChatRequest):
    logger.info(f"CHAT: model={req.chat_model_id[:30]} msg={req.message[:100]}")
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
    return {"skills": skill_registry.to_api_list(),
            "count": len(skill_registry.get_all()),
            "enabled_count": len(skill_registry.get_enabled())}

@app.get("/api/skills/{name}")
async def get_skill_detail(name: str):
    skill = skill_registry.get(name)
    if not skill:
        return {"error": f"Skill '{name}' not found"}
    matches = [s for s in skill_registry.to_api_list() if s["name"] == name]
    return matches[0] if matches else {"error": f"Skill '{name}' not found"}


# ── SSE Helper ──

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── State ──

def _make_state(req: ChatRequest) -> dict:
    return {
        "messages": [], "user_request": req.message, "tasks": [],
        "image_results": [], "video_results": [], "research_results": [],
        "analyst_results": [], "code_results": [], "chart_results": [],
        "skill_outputs": {},
        "final_response": "",
        "chat_model_id": req.chat_model_id, "image_model_id": req.image_model_id,
        "video_model_id": req.video_model_id, "router_model_id": req.router_model_id,
        "_skip_synthesizer": False,
    }


# ── Sync Run ──

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


# ── Streaming ──

async def _stream_chat(req: ChatRequest) -> AsyncGenerator[str, None]:
    """Stream agent progress via SSE. Graph's synthesizer provides token-level streaming."""
    initial_state = _make_state(req)

    yield _sse("phase", {"phase": "supervisor", "message": "分析请求，规划任务..."})

    accumulated: dict[str, list] = {}
    emitted_agents: set = set()
    collected_html: list[dict] = []
    full_response = ""
    direct_response_sent = False

    try:
        async for event in get_multi_agent_graph().astream_events(initial_state):
            kind = event.get("event", "")
            name = event.get("name", "")
            data = event.get("data", {})

            # Chat model stream (synthesizer tokens)
            if kind == "on_chat_model_stream":
                chunk = data.get("chunk", {})
                if hasattr(chunk, "content") and chunk.content:
                    text = chunk.content
                    full_response += text
                    yield _sse("token", {"text": text})

            # Supervisor completed
            elif kind == "on_chain_end" and name == "supervisor":
                output = _extract_output(data)
                tasks = output.get("tasks", [])

                if output.get("final_response") and not tasks:
                    yield _sse("phase", {"phase": "done", "message": "直接回复"})
                    yield _sse("final", {"response": output["final_response"]})
                    yield _sse("done", {})
                    direct_response_sent = True
                    return

                if not tasks:
                    yield _sse("final", {"response": "无法理解请求"})
                    yield _sse("done", {})
                    direct_response_sent = True
                    return

                yield _sse("plan", {
                    "tasks": [{"agent": t.get("agent", ""), "prompt": t.get("prompt", "")[:80]} for t in tasks],
                    "count": len(tasks),
                })

            # Worker started
            elif kind == "on_chain_start" and name in WORKER_NODES:
                if name not in emitted_agents:
                    emitted_agents.add(name)
                    yield _sse("agent_start", {
                        "agent": name,
                        "label": AGENT_LABELS.get(name, name),
                    })

            # Worker completed
            elif kind == "on_chain_end" and name in WORKER_NODES:
                yield _sse("agent_done", {
                    "agent": name,
                    "label": AGENT_LABELS.get(name, name),
                })
                output = _extract_output(data)
                for key in RESULT_KEYS:
                    if key in output and output[key]:
                        accumulated.setdefault(key, []).extend(
                            output[key] if isinstance(output[key], list) else [output[key]]
                        )
                    # Handle skill HTML outputs
                    if key == "skill_outputs" and isinstance(output.get(key), dict):
                        for skill_name, skill_items in output[key].items():
                            for item in (skill_items or []):
                                if isinstance(item, dict):
                                    result_text = item.get("result", "")
                                    if isinstance(result_text, str) and _HTML_DETECT_RE.search(result_text[:500]):
                                        saved = _save_skill_html(skill_name, result_text,
                                            title=item.get("task", ""))
                                        item["html_url"] = saved["html_url"]
                                        item["html_title"] = saved["title"]
                                        collected_html.append({
                                            "skill_name": skill_name,
                                            "html_url": saved["html_url"],
                                            "title": saved["title"],
                                            "task": item.get("task", ""),
                                        })

            # Synthesizer completed (fallback if no streaming tokens)
            elif kind == "on_chain_end" and name == "synthesizer":
                output = _extract_output(data)
                if output.get("final_response") and not full_response:
                    full_response = output["final_response"]

    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        logger.error(f"Graph streaming error: {e}")
        yield _sse("error", {"error": f"请求被中断: {str(e) if str(e) else type(e).__name__}"})
        yield _sse("done", {})
        return

    if direct_response_sent:
        return

    yield _sse("final", {
        "response": full_response,
        "chart_results": accumulated.get("chart_results", []),
        "image_results": accumulated.get("image_results", []),
        "html_results": collected_html,
    })
    yield _sse("done", {})


def _extract_output(data: dict) -> dict:
    """Extract node output from astream_events event data."""
    out = data.get("output")
    if isinstance(out, dict):
        return out
    chunk = data.get("chunk")
    if isinstance(chunk, dict):
        return chunk
    return {}


# ── Main ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
