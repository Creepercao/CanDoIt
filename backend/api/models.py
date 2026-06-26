"""Model and provider routes."""
from fastapi import APIRouter
from backend.config import PROVIDERS
from backend.models.registry import registry

router = APIRouter(tags=["models"])


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


@router.get("/providers")
async def list_providers():
    return [
        {
            "name": p.name,
            "type": p.type,
            "base_url": p.base_url,
            "api_key_masked": p.api_key[:8] + "***" + p.api_key[-4:],
        }
        for p in PROVIDERS
    ]


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
