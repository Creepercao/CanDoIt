"""PPT run recovery APIs."""

from fastapi import APIRouter
from pydantic import BaseModel

from backend.agents.builtin_workers import ppt_assembler_worker, ppt_slide_worker
from backend.agents.orchestrator import merge_worker_results, make_initial_state
from backend.tools.ppt_run_store import read_manifest

router = APIRouter(tags=["ppt-runs"])


class PPTSlideRegenerateRequest(BaseModel):
    chat_model_id: str = ""
    prompt: str = ""


@router.get("/ppt-runs/{deck_id}")
async def get_ppt_run(deck_id: str):
    manifest = read_manifest(deck_id)
    if not manifest:
        return {"error": "PPT run not found"}
    return manifest


@router.post("/ppt-runs/{deck_id}/slides/{slide_index}/regenerate")
async def regenerate_ppt_slide(deck_id: str, slide_index: int, req: PPTSlideRegenerateRequest):
    manifest = read_manifest(deck_id)
    if not manifest or not manifest.get("plan"):
        return {"error": "PPT run not found"}

    plan = manifest["plan"]
    slide_spec = next(
        (s for s in plan.get("slides", []) if int(s.get("index", 0) or 0) == slide_index),
        None,
    )
    if not slide_spec:
        return {"error": f"Slide {slide_index} not found in plan"}

    state = make_initial_state(
        user_request=req.prompt or plan.get("title", "regenerate PPT slide"),
        chat_model_id=req.chat_model_id,
    )
    state["skill_outputs"] = {"ppt_planner": [plan]}
    state["tasks"] = [{
        "agent": "ppt_slide",
        "id": f"ppt_slide:{slide_index}",
        "deck_id": deck_id,
        "slide_index": slide_index,
        "slide_spec": slide_spec,
        "prompt": req.prompt or f"Regenerate slide {slide_index}: {slide_spec.get('title', '')}",
    }]

    slide_result = await ppt_slide_worker(state)
    merge_worker_results(state, [slide_result])
    state["tasks"] = [{
        "agent": "ppt_assembler",
        "id": "ppt_assembler:repair",
        "deck_id": deck_id,
        "prompt": "Reassemble PPT deck after one slide regeneration",
    }]
    assembled = await ppt_assembler_worker(state)
    merge_worker_results(state, [assembled])

    deck_output = (state.get("skill_outputs", {}).get("ppt-animation") or [{}])[-1]
    return {
        "success": True,
        "deck_id": deck_id,
        "slide_index": slide_index,
        "slide": (state.get("skill_outputs", {}).get("ppt_slide") or [{}])[-1],
        "html_url": deck_output.get("html_url", ""),
        "html_title": deck_output.get("html_title", ""),
        "failed_slides": deck_output.get("failed_slides", []),
    }
