from __future__ import annotations

from typing import Any

from docsift.core.config import EvalProfileName, EvalProviderConfig, Settings
from docsift.services.llm.providers import (
    LLMProviderProtocol,
    OllamaProvider,
    OpenAICompatibleProvider,
)


def _build_provider(
    config: EvalProviderConfig,
    settings: Settings | None = None,
) -> LLMProviderProtocol:
    if not config.base_url.strip():
        raise ValueError(f"Base URL is not configured for eval profile '{config.profile}'")
    if not config.model.strip():
        raise ValueError(f"Model is not configured for eval profile '{config.profile}'")

    common = {
        "base_url": config.base_url,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "native_structured_output": config.native_structured_output,
    }
    if config.provider == "ollama":
        extra: dict[str, Any] = {}
        if settings is not None:
            extra = {
                "num_predict": settings.llm_num_predict,
                "num_ctx": settings.llm_num_ctx,
                "max_concurrency": settings.llm_max_concurrency,
            }
        return OllamaProvider(**common, **extra)
    api_key = (
        config.api_key.get_secret_value() if config.api_key is not None else None
    )
    extra_oai: dict[str, Any] = {}
    if settings is not None:
        extra_oai = {"max_concurrency": settings.llm_max_concurrency}
    return OpenAICompatibleProvider(api_key=api_key, **common, **extra_oai)


def build_llm_provider(settings: Settings) -> LLMProviderProtocol:
    config = EvalProviderConfig(
        profile="local",
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        native_structured_output=settings.llm_native_structured_output,
        timeout_seconds=settings.llm_timeout_seconds,
        input_price_per_million=0,
        output_price_per_million=0,
    )
    return _build_provider(config, settings)


def build_eval_llm_provider(
    settings: Settings,
    profile: EvalProfileName,
) -> LLMProviderProtocol:
    return _build_provider(settings.eval_profile(profile))
