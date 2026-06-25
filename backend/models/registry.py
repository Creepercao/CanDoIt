"""Model registry — hardcoded model list per provider."""
from typing import NamedTuple
from backend.config import PROVIDERS, ProviderConfig

IMAGE_KEYWORDS = ["stable", "diffusion", "sd", "image", "flux", "dall-e", "midjourney",
                   "sdxl", "sd3", "playground"]
VIDEO_KEYWORDS = ["video", "svd", "animate", "runway", "cogvideo", "cogvideox",
                   "videocraft", "zeroscope", "kling"]


def _classify(model_id: str) -> str:
    lower = model_id.lower()
    for kw in VIDEO_KEYWORDS:
        if kw in lower:
            return "video"
    for kw in IMAGE_KEYWORDS:
        if kw in lower:
            return "image"
    return "chat"


class ModelInfo(NamedTuple):
    id: str
    provider: str
    type: str  # chat, image, video


class ModelRegistry:
    """Hardcoded model registry — add known models here or let the frontend
    use the provider's /models endpoint directly."""

    # Default models known to work — add more as needed
    _DEFAULT_MODELS: dict[str, list[ModelInfo]] = {}

    def __init__(self):
        self._models: dict[str, list[ModelInfo]] = dict(self._DEFAULT_MODELS)
        self._fetched = False

    def _ensure_defaults(self):
        """Populate with sensible defaults per provider if no models loaded."""
        if not self._models and PROVIDERS:
            for p in PROVIDERS:
                models = [
                    ModelInfo("deepseek-ai/DeepSeek-V3", p.name, "chat"),
                    ModelInfo("stabilityai/stable-diffusion-3-5-large", p.name, "image"),
                ]
                self._models[p.name] = models

    async def refresh(self):
        """Load hardcoded models (no HTTP fetch)."""
        self._ensure_defaults()
        self._fetched = True

    def list_models(self, model_type: str = "") -> list[ModelInfo]:
        self._ensure_defaults()
        result = []
        for provider_models in self._models.values():
            for m in provider_models:
                if not model_type or m.type == model_type:
                    result.append(m)
        return result

    def get_all(self) -> list[ModelInfo]:
        return self.list_models()

    def get_by_type(self, model_type: str) -> list[ModelInfo]:
        return self.list_models(model_type)

    def get_chat_models(self) -> list[ModelInfo]:
        return self.list_models("chat")

    def get_image_models(self) -> list[ModelInfo]:
        return self.list_models("image")

    def get_video_models(self) -> list[ModelInfo]:
        return self.list_models("video")

    def find_provider_config(self, model_id: str) -> ProviderConfig | None:
        self._ensure_defaults()
        for provider in PROVIDERS:
            if provider.name in self._models:
                for m in self._models[provider.name]:
                    if m.id == model_id:
                        return provider
        return PROVIDERS[0] if PROVIDERS else None


registry = ModelRegistry()
