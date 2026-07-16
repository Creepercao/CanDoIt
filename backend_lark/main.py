"""Feishu/Lark dedicated FastAPI backend.

Run with:
    python -m uvicorn backend_lark.main:app --host 0.0.0.0 --port 8001 --reload
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartParser

from backend.api import OUTPUTS_DIR, SKILL_HTML_DIR
from backend.api.ppt_runs import router as ppt_runs_router
from backend.api.pptx import router as pptx_router
from backend_lark.agent_service import router as agent_router
from backend_lark.feishu_events import router as event_router

MultiPartParser.max_file_size = 200 * 1024 * 1024
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend_lark")

app = FastAPI(
    title="CanDoIt Feishu/Lark Backend",
    version="1.0.0",
    description="Dedicated Feishu/Lark adapter backend built on CanDoIt Agent and Skill runtime.",
)

OUTPUTS_DIR.mkdir(exist_ok=True)
SKILL_HTML_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router, prefix="/api")
app.include_router(event_router, prefix="/api")
app.include_router(pptx_router, prefix="/api")
app.include_router(ppt_runs_router, prefix="/api")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "backend_lark",
        "mode": "feishu-dedicated",
    }


@app.on_event("startup")
async def startup() -> None:
    from backend.agents.agent_loader import load_builtin_agents
    from backend.mcp_adapter import register_mcp_skills
    from backend.models.registry import registry
    from backend.research_cache import get_knowledge_base, get_research_cache
    from backend.skills.package_installer import restore_from_disk
    from backend.skills.registry import skill_registry

    await registry.refresh()
    skill_registry.discover()
    skill_registry.register_builtins(load_builtin_agents())
    restore_from_disk(skill_registry)
    await register_mcp_skills(skill_registry)

    for warning in skill_registry.validate_dependencies():
        logger.warning("Dependency warning: %s", warning)

    get_research_cache()
    kb = get_knowledge_base()
    if kb:
        await kb._ensure_init()

    redis_status = "enabled" if os.environ.get("REDIS_URL") else "disabled"
    logger.info(
        "Feishu backend loaded %s models | %s skills | Redis: %s",
        len(registry.get_all()),
        len(skill_registry.get_enabled()),
        redis_status,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend_lark.main:app", host="0.0.0.0", port=8001, reload=True)

