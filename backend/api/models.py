"""Model and provider routes."""
import logging
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import PROVIDERS, ProviderConfig, save_providers_safe, reload_providers
from backend.models.registry import registry

router = APIRouter(tags=["models"])
logger = logging.getLogger("api.models")


# ── Pydantic request models ──────────────────────────────────────────


class ProviderCreateRequest(BaseModel):
    name: str
    type: Literal["llm", "image", "video", "search"] = "llm"
    base_url: str = ""
    api_key: str = ""


class ProviderUpdateRequest(BaseModel):
    type: str = ""
    base_url: str = ""
    api_key: str = ""  # sentinel: if contains "***", keep existing key


# ── Health ────────────────────────────────────────────────────────────


@router.get("/health")
async def health():
    from backend.cache import cache
    from backend.skills.registry import skill_registry
    return {
        "status": "ok",
        "models": len(registry.get_all()),
        "providers": len(PROVIDERS),
        "skills": len(skill_registry.get_enabled()),
        "cache": cache.backend,
        "redis": "enabled" if cache.backend == "redis" else "fallback-memory",
    }


def _mask_key(key: str) -> str:
    """Safely mask an API key for display: first 8 + *** + last 4."""
    if len(key) <= 8:
        return key[:2] + "***" + (key[-2:] if len(key) >= 4 else "")
    return key[:8] + "***" + key[-4:]


@router.get("/providers")
async def list_providers():
    return [
        {
            "name": p.name,
            "type": p.type,
            "base_url": p.base_url,
            "api_key_masked": _mask_key(p.api_key),
        }
        for p in PROVIDERS
    ]


@router.post("/providers")
async def create_provider(req: ProviderCreateRequest):
    try:
        providers = list(PROVIDERS)
        if any(p.name == req.name for p in providers):
            return {"error": f"Provider '{req.name}' already exists"}
        new_provider = ProviderConfig(
            name=req.name, type=req.type, base_url=req.base_url, api_key=req.api_key,
        )
        providers.append(new_provider)
        await save_providers_safe(providers)
        await registry.refresh()
        logger.info(f"Provider created: {req.name} (type={req.type})")
        return {"success": True, "name": req.name}
    except Exception as e:
        logger.error(f"Failed to create provider '{req.name}': {e}", exc_info=True)
        return {"error": f"Failed to save provider: {e}"}


@router.put("/providers/{name}")
async def update_provider(name: str, req: ProviderUpdateRequest):
    try:
        providers = list(PROVIDERS)
        for i, p in enumerate(providers):
            if p.name == name:
                updated = p.model_dump()
                if req.type:
                    updated["type"] = req.type
                if req.base_url:
                    updated["base_url"] = req.base_url
                if req.api_key and "***" not in req.api_key:
                    updated["api_key"] = req.api_key
                providers[i] = ProviderConfig(**updated)
                await save_providers_safe(providers)
                await registry.refresh()
                logger.info(f"Provider updated: {name}")
                return {"success": True, "name": name}
        return {"error": f"Provider '{name}' not found"}
    except Exception as e:
        logger.error(f"Failed to update provider '{name}': {e}", exc_info=True)
        return {"error": f"Failed to update provider: {e}"}


@router.delete("/providers/{name}")
async def delete_provider(name: str):
    try:
        providers = list(PROVIDERS)
        new_list = [p for p in providers if p.name != name]
        if len(new_list) == len(providers):
            return {"error": f"Provider '{name}' not found"}
        await save_providers_safe(new_list)
        await registry.refresh()
        logger.info(f"Provider deleted: {name}")
        return {"success": True, "name": name}
    except Exception as e:
        logger.error(f"Failed to delete provider '{name}': {e}", exc_info=True)
        return {"error": f"Failed to delete provider: {e}"}


@router.get("/models")
async def list_models(type: str = ""):
    if not registry._fetched:
        await registry.refresh()
    models = registry.get_all() if not type else registry.get_by_type(type)
    return [
        {"id": m.id, "provider": m.provider, "type": m.type} for m in models
    ]


@router.get("/models/chat")
async def list_chat_models():
    return [
        {"id": m.id, "provider": m.provider} for m in registry.get_chat_models()
    ]


@router.get("/models/image")
async def list_image_models():
    return [
        {"id": m.id, "provider": m.provider} for m in registry.get_image_models()
    ]


@router.get("/models/video")
async def list_video_models():
    return [
        {"id": m.id, "provider": m.provider} for m in registry.get_video_models()
    ]


@router.post("/refresh-models")
async def refresh_models():
    await registry.refresh()
    return {
        "total": len(registry.get_all()),
        "chat": len(registry.get_chat_models()),
        "image": len(registry.get_image_models()),
        "video": len(registry.get_video_models()),
    }
