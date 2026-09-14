"""Model provider abstraction — create LangChain models from config."""
from langchain_openai import ChatOpenAI
from backend.config import PROVIDERS, ProviderConfig, get_provider_by_type
from backend.models.registry import registry


def _run_callbacks() -> list:
    """Attach the usage collector when a run trace is active."""
    try:
        from backend.observability.trace import build_usage_callback, current_trace

        if current_trace() is None:
            return []
        callback = build_usage_callback()
        return [callback] if callback is not None else []
    except Exception:  # tracing must never break model creation
        return []


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
        provider_config = get_provider_by_type("llm")

    return ChatOpenAI(
        model=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_api_key=provider_config.api_key,
        openai_api_base=provider_config.base_url,
        callbacks=_run_callbacks(),
    )


def get_default_chat_model() -> ChatOpenAI:
    """Get default chat model from first provider."""
    chat_models = registry.get_chat_models()
    if chat_models:
        return create_chat_model(chat_models[0].id)
    # Fallback
    return create_chat_model("deepseek-ai/DeepSeek-V3")
