"""Skill listing, detail, and toggle routes."""
import logging
from fastapi import APIRouter
from backend.skills.registry import skill_registry
from backend.agents.graph import rebuild_graph
from backend.skills.package_installer import save_package_state

router = APIRouter(tags=["skills"])
logger = logging.getLogger("api.skills")


@router.get("/skills")
async def list_skills():
    return {
        "skills": skill_registry.to_api_list(),
        "count": len(skill_registry.get_all()),
        "enabled_count": len(skill_registry.get_enabled()),
    }


@router.get("/skills/{name}")
async def get_skill_detail(name: str):
    skill = skill_registry.get(name)
    if not skill:
        return {"error": f"Skill '{name}' not found"}
    matches = [s for s in skill_registry.to_api_list() if s["name"] == name]
    return matches[0] if matches else {"error": f"Skill '{name}' not found"}


@router.post("/skills/{name}/toggle")
async def toggle_skill(name: str, enabled: bool = True):
    skill = skill_registry.get(name)
    if not skill:
        return {"error": f"Skill '{name}' not found"}
    skill.enabled = enabled
    rebuild_graph()
    if skill._package_meta and skill._package_meta.get("source") == "package":
        save_package_state(name, enabled)
    logger.info(f"Skill '{name}' {'enabled' if enabled else 'disabled'} - graph rebuilt")
    return {"name": name, "enabled": enabled, "graph_rebuilt": True}
