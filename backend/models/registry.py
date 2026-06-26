"""Model registry — auto-discover available models from providers."""
import httpx
from typing import Any
from backend.config import PROVIDERS, ProviderConfig, get_provider_by_type


class ModelInfo:
    def __init__(self, model_id: str, provider: str, model_type: str = "chat"):
        self.id = model_id
        self.provider = provider
        self.type = model_type  # chat, image, video


class ModelRegistry:
    """Auto-fetch available models from each provider."""

    IMAGE_KEYWORDS = ["stable", "diffusion", "sd", "image", "flux", "dall-e", "midjourney",
                       "sdxl", "sd3", "playground"]
    VIDEO_KEYWORDS = ["video", "svd", "animate", "runway", "cogvideo", "cogvideox",
                       "videocraft", "zeroscope", "kling"]

    def __init__(self):
        self.models: dict[str, list[ModelInfo]] = {}  # provider -> models
        self._fetched = False

    async def fetch_models(self, provider: ProviderConfig) -> list[ModelInfo]:
        """Fetch available models from a provider's /models endpoint."""
        models: list[ModelInfo] = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{provider.base_url}/models",
                    headers={"Authorization": f"Bearer {provider.api_key}"}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    model_list = data.get("data", []) or data.get("models", [])
                    for item in model_list:
                        model_id = item.get("id", "") or item.get("name", "")
                        if not model_id:
                            continue
                        mtype = self._classify(model_id)
                        models.append(ModelInfo(model_id, provider.name, mtype))
        except Exception:
            pass
        return models

    def _classify(self, model_id: str) -> str:
        lower = model_id.lower()
        for kw in self.VIDEO_KEYWORDS:
            if kw in lower:
                return "video"
        for kw in self.IMAGE_KEYWORDS:
            if kw in lower:
                return "image"
        return "chat"

    async def refresh(self):
        """Fetch all models from all providers.

        Only queries LLM and image/video providers (types that expose a
        ``/models`` endpoint).  Search-type providers are skipped.
        """
        self.models.clear()
        for provider in PROVIDERS:
            if provider.type == "search":
                continue  # search APIs don't expose /models
            self.models[provider.name] = await self.fetch_models(provider)
        self._fetched = True

    def get_all(self) -> list[ModelInfo]:
        result = []
        for provider_models in self.models.values():
            result.extend(provider_models)
        return result

    def get_by_type(self, model_type: str) -> list[ModelInfo]:
        return [m for m in self.get_all() if m.type == model_type]

    def get_chat_models(self) -> list[ModelInfo]:
        return self.get_by_type("chat")

    def get_image_models(self) -> list[ModelInfo]:
        return self.get_by_type("image")

    def get_video_models(self) -> list[ModelInfo]:
        return self.get_by_type("video")

    def find_provider_config(self, model_id: str) -> ProviderConfig | None:
        for provider in PROVIDERS:
            if provider.name in self.models:
                for m in self.models[provider.name]:
                    if m.id == model_id:
                        return provider
        # If model not found in registry, return LLM provider as fallback
        return get_provider_by_type("llm")


registry = ModelRegistry()
