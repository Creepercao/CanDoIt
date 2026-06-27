"""Chat, image generation, and video generation routes + streaming orchestrator."""
import json
import logging
import re as _re
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from langchain_core.messages import HumanMessage

from backend.config import PROVIDERS
from backend.models.registry import registry
from backend.models.provider import create_chat_model, get_default_chat_model
from backend.agents.orchestrator import (
    run_agent_loop,
    run_agent_loop_stream,
    make_initial_state,
    merge_worker_results,
    supervisor_node,
)
from backend.tools.image_gen import generate_image
from backend.tools.video_gen import generate_video
from backend.skills.registry import skill_registry
from backend.prompts import SYNTHESIZER_PROMPT_TEMPLATE
from backend.api import (
    logger as _parent_logger,
    sse as _sse,
    resolve_output_url as _resolve_output_url,
    save_skill_html as _save_skill_html,
    _HTML_DETECT_RE,
)

router = APIRouter(tags=["chat"])
logger = logging.getLogger("api.chat")


# ---- Request models ----

class ChatRequest(BaseModel):
    message: str
    chat_model_id: str = ""
    image_model_id: str = ""
    video_model_id: str = ""
    router_model_id: str = ""
    session_id: str = ""
    history: list[dict] = []
    stream: bool = True


class ImageGenRequest(BaseModel):
    prompt: str
    model_id: str = ""
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 20


class VideoGenRequest(BaseModel):
    prompt: str
    model_id: str = ""
    duration: int = 5


# ---- Agent helpers (dynamic, depends on skill_registry) ----

def _get_agent_labels() -> dict[str, str]:
    """Build agent label map from registry (covers built-in + skill agents)."""
    base = {
        "supervisor": "supervisor",
        "synthesizer": "synthesizer",
    }
    # Built-in agents — get their display names from the registry
    for name, skill in skill_registry.get_all().items():
        if skill_registry._has_worker(skill):
            base[skill.node_name] = f"{skill.emoji} {skill.display_name}"
    # Skill agent labels (may override)
    base.update(skill_registry.get_agent_labels())
    return base


def _get_agent_map() -> dict[str, str]:
    """Build agent→node mapping from registry (covers both built-in and skills)."""
    return skill_registry.get_node_for_agent()


def _get_worker_nodes() -> set[str]:
    """Get all worker node names from the registry (covers built-in + skills)."""
    return set(skill_registry.get_node_funcs().keys())


def _get_result_keys() -> tuple[str, ...]:
    return (
        "research_results", "analyst_results", "chart_results",
        "image_results", "video_results", "code_results",
        "skill_outputs",
    )


def _agent_type_from_node(node_name: str) -> str:
    agent_map = _get_agent_map()
    for agent_type, mapped_node in agent_map.items():
        if mapped_node == node_name:
            return agent_type
    return node_name


def _tasks_for_node(tasks: list[dict], node_name: str) -> list[dict]:
    agent_type = _agent_type_from_node(node_name)
    return [t for t in tasks if t.get("agent") == agent_type]


def _summarize_worker_output(output: dict) -> dict:
    for key in _get_result_keys():
        value = output.get(key)
        if not value:
            continue
        if key == "skill_outputs" and isinstance(value, dict):
            names = list(value.keys())
            total = sum(len(items or []) for items in value.values())
            return {"result_key": key, "summary": f"generated {total} skill results", "count": total, "skills": names}
        items = value if isinstance(value, list) else [value]
        if key == "research_results":
            sources = sum(len(item.get("sources") or []) for item in items if isinstance(item, dict))
            return {"result_key": key, "summary": f"found {sources or len(items)} sources", "count": len(items)}
        if key == "analyst_results":
            structured = sum(1 for item in items if isinstance(item, dict) and item.get("structured_data"))
            return {"result_key": key, "summary": "structured analysis complete" if structured else "analysis complete", "count": len(items)}
        if key == "chart_results":
            charts = sum(1 for item in items if isinstance(item, dict) and (item.get("result") or {}).get("url"))
            return {"result_key": key, "summary": f"generated {charts} charts" if charts else "chart task done", "count": len(items)}
        if key == "image_results":
            images = sum(1 for item in items if isinstance(item, dict) and (item.get("result") or {}).get("url"))
            return {"result_key": key, "summary": f"generated {images} images" if images else "image task done", "count": len(items)}
        if key == "video_results":
            return {"result_key": key, "summary": "video task done", "count": len(items)}
        if key == "code_results":
            return {"result_key": key, "summary": "code task done", "count": len(items)}
    return {"summary": "task done", "count": 0}


# ---- State helpers ----

def _make_state(req: ChatRequest, skip_synthesizer: bool = False) -> dict:
    state = make_initial_state(
        user_request=req.message,
        chat_model_id=req.chat_model_id,
        image_model_id=req.image_model_id,
        video_model_id=req.video_model_id,
        router_model_id=req.router_model_id,
        history=req.history,
    )
    state["_skip_synthesizer"] = skip_synthesizer
    return state


def _extract_output(data: dict) -> dict:
    out = data.get("output")
    if isinstance(out, dict):
        return out
    chunk = data.get("chunk")
    if isinstance(chunk, dict):
        return chunk
    return {}


def _build_synth_prompt(user_request: str, state: dict, history: list = None) -> str:
    """Build synthesizer prompt from accumulated worker results and conversation history."""
    history_prefix = ""
    if history and len(history) > 0:
        recent = history[-10:]
        lines = []
        for m in recent:
            role = m.get("role", "user")
            text = m.get("content", "")
            if len(text) > 300:
                text = text[:300] + "..."
            lines.append(f"[{role}]: {text}")
        history_prefix = "Conversation history:\n" + "\n".join(lines) + "\n\n"

    research = json.dumps(state.get("research_results", []), ensure_ascii=False, indent=2)
    analyst = json.dumps(state.get("analyst_results", []), ensure_ascii=False, indent=2)
    charts = json.dumps(state.get("chart_results", []), ensure_ascii=False, indent=2)
    images = json.dumps(state.get("image_results", []), ensure_ascii=False, indent=2)
    videos = json.dumps(state.get("video_results", []), ensure_ascii=False, indent=2)
    codes = json.dumps(state.get("code_results", []), ensure_ascii=False, indent=2)
    skill_outputs_json = json.dumps(state.get("skill_outputs", {}), ensure_ascii=False, indent=2)

    table_blocks = ""
    for cr in state.get("chart_results", []):
        if cr.get("table_markdown"):
            title = cr.get("chart_spec", {}).get("title", "table data")
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

    return history_prefix + SYNTHESIZER_PROMPT_TEMPLATE.format(
        user_request=user_request,
        research=research if research != '[]' else 'None',
        analyst=analyst if analyst != '[]' else 'None',
        charts=charts if charts != '[]' else 'None',
        images=images if images != '[]' else 'None',
        videos=videos if videos != '[]' else 'None',
        codes=codes if codes != '[]' else 'None',
        skill_outputs=skill_outputs_json if skill_outputs_json != '{}' else 'None',
        table_blocks=table_blocks if table_blocks else "(no tables generated)",
        html_links=html_links_section,
    )


# ---- Sync run ----

async def _run_agents(req: ChatRequest) -> dict:
    state = _make_state(req)
    try:
        result = await run_agent_loop(state)
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
        logger.error(f"Agent loop error: {e}")
        return {"success": False, "response": f"Error: {str(e)}", "error": str(e)}


# ---- Streaming orchestrator ----

async def _stream_chat(req: ChatRequest) -> AsyncGenerator[str, None]:
    state = _make_state(req, skip_synthesizer=True)
    chat_id = req.chat_model_id or ""

    yield _sse("phase", {"phase": "supervisor", "message": "analyzing request..."})

    agent_labels = _get_agent_labels()
    agent_map = _get_agent_map()
    collected_html: list[dict] = []
    accumulated: dict[str, list] = {}

    try:
        async for event in run_agent_loop_stream(state, agent_labels, agent_map):
            ev_type = event.get("event", "")
            data = event.get("data", {})

            if ev_type == "phase":
                yield _sse("phase", data)

            elif ev_type == "plan":
                yield _sse("plan", data)

            elif ev_type == "agent_start":
                yield _sse("agent_start", data)

            elif ev_type == "agent_done":
                yield _sse("agent_done", data)

            elif ev_type == "final":
                yield _sse("final", data)
                yield _sse("done", {})
                return

            elif ev_type == "done":
                yield _sse("done", {})
                return

            elif ev_type == "_agent_loop_done":
                # Agent loop finished — extract accumulated state for synthesizer
                state = data.get("state", state)
                accumulated = data.get("accumulated", {})
                collected_html = data.get("collected_html", [])

    except Exception as e:
        logger.error(f"Agent loop streaming error: {e}")
        yield _sse("error", {"error": str(e)})
        yield _sse("done", {})
        return

    yield _sse("phase", {"phase": "synthesize", "message": "synthesizing..."})

    synth_prompt = _build_synth_prompt(req.message, state, req.history)
    full_response = ""

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

    yield _sse("final", {
        "response": full_response,
        "chart_results": state.get("chart_results", []),
        "image_results": state.get("image_results", []),
        "html_results": collected_html,
    })
    yield _sse("done", {})


# ---- Routes ----

@router.post("/chat")
async def chat(req: ChatRequest):
    logger.info(f"CHAT REQ: model={req.chat_model_id[:30]} msg={req.message[:100]}")
    if req.stream:
        return StreamingResponse(
            _stream_chat(req),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await _run_agents(req)


@router.post("/chat/sync")
async def chat_sync(req: ChatRequest):
    return await _run_agents(req)


@router.post("/generate-image")
async def api_generate_image(req: ImageGenRequest):
    return await generate_image(
        prompt=req.prompt,
        model_id=req.model_id or "stabilityai/stable-diffusion-3-5-large",
        negative_prompt=req.negative_prompt,
        width=req.width, height=req.height, steps=req.steps,
    )


@router.post("/generate-video")
async def api_generate_video(req: VideoGenRequest):
    return await generate_video(
        prompt=req.prompt, model_id=req.model_id, duration=req.duration,
    )
