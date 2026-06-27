"""FastAPI server - main entry point (router assembly only)."""
import os
import logging

from starlette.formparsers import MultiPartParser

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api import OUTPUTS_DIR, SKILL_HTML_DIR
from backend.api.models import router as models_router
from backend.api.chat import router as chat_router
from backend.api.skills import router as skills_router
from backend.api.packages import router as packages_router
from backend.api.sessions import router as sessions_router
from backend.api.research_cache import router as knowledge_router
from backend.api.pptx import router as pptx_router

# Allow up to 200 MB file uploads (skill packages)
MultiPartParser.max_file_size = 200 * 1024 * 1024

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Multi-Agent Platform", version="1.0.0")

# Static files & outputs
OUTPUTS_DIR.mkdir(exist_ok=True)
SKILL_HTML_DIR.mkdir(exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(models_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(skills_router, prefix="/api")
app.include_router(packages_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")
app.include_router(pptx_router, prefix="/api")


@app.on_event("startup")
async def startup():
    from backend.models.registry import registry
    from backend.skills.registry import skill_registry
    from backend.skills.package_installer import restore_from_disk
    from backend.agents.agent_loader import load_builtin_agents
    from backend.agents.graph import get_multi_agent_graph

    await registry.refresh()
    skill_registry.discover()
    skill_registry.register_builtins(load_builtin_agents())
    restore_from_disk(skill_registry)

    # Validate agent dependency chains
    warnings = skill_registry.validate_dependencies()
    for w in warnings:
        logger.warning(f"Dependency warning: {w}")

    get_multi_agent_graph()  # build graph after skill discovery

    # ── Initialize research cache + knowledge base ──
    from backend.research_cache import get_knowledge_base, get_research_cache
    get_research_cache()  # lazy init
    kb = get_knowledge_base()  # lazy init — triggers ChromaDB load
    if kb:
        await kb._ensure_init()

    redis_status = "enabled" if os.environ.get("REDIS_URL") else "disabled"
    logger = logging.getLogger("api")
    logger.info(
        f"Loaded {len(registry.get_all())} models | "
        f"{len(skill_registry.get_enabled())} skills | "
        f"Redis: {redis_status}"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
