"""Skill package management routes."""
import logging
from fastapi import APIRouter, UploadFile, File
from backend.skills.registry import skill_registry
from backend.agents.graph import rebuild_graph
from backend.skills.package_installer import (
    install_from_zip, uninstall_package, _load_packages_json, get_readme as _get_readme,
)

router = APIRouter(tags=["packages"])
logger = logging.getLogger("api.packages")


@router.post("/skills/packages/install")
async def install_skill_package(file: UploadFile = File(...)):
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


@router.delete("/skills/packages/{name}")
async def uninstall_skill_package(name: str):
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


@router.get("/skills/packages")
async def list_packages():
    packages = _load_packages_json()
    return {"packages": packages, "count": len(packages)}


@router.get("/skills/{name}/readme")
async def get_skill_readme(name: str):
    content = skill_registry.get_readme(name)
    if content is None:
        return {"error": f"No readme found for skill '{name}'"}
    return {"name": name, "content": content}
