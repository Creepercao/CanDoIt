"""Model provider abstraction — create LangChain models from config."""
from langchain_openai import ChatOpenAI
from backend.config import PROVIDERS, ProviderConfig
from backend.models.registry import registry


def create_chat_model(
    model_id: str = "deepseek-ai/DeepSeek-V3",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    provider_config: ProviderConfig | None = None,
) -> ChatOpenAI:
    """Create a LangChain ChatOpenAI connected to the provider."""
    if provider_config is None:
        provider_config = registry.find_provider_config(model_id)
    if provider_config is None:
        provider_config = PROVIDERS[0]

    return ChatOpenAI(
        model=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_api_key=provider_config.apikey,
        openai_api_base=provider_config.base_url,
        request_timeout=180,  # 3 min timeout for long generations
    )


def get_default_chat_model() -> ChatOpenAI:
    """Get default chat model from first provider."""
    chat_models = registry.get_chat_models()
    if chat_models:
        return create_chat_model(chat_models[0].id)
    # Fallback
    return create_chat_model("deepseek-ai/DeepSeek-V3")
