from decimal import Decimal

import pytest

from docsift.core.config import Settings
from docsift.services.llm.factory import build_eval_llm_provider, build_llm_provider


def test_local_eval_profile_has_free_default_model() -> None:
    settings = Settings(_env_file=None)

    profile = settings.eval_profile("local")
    provider = build_eval_llm_provider(settings, "local")

    assert profile.model == "qwen2.5-coder:7b"
    assert profile.input_price_per_million == Decimal(0)
    assert profile.output_price_per_million == Decimal(0)
    assert provider.provider_name == "ollama"
    assert provider.model_name == "qwen2.5-coder:7b"


def test_cloud_eval_profile_uses_configured_provider_model_and_prices() -> None:
    settings = Settings(
        _env_file=None,
        eval_cloud_base_url="https://llm.example.test/v1",
        eval_cloud_model="example-model",
        eval_cloud_api_key="not-a-real-key",
        eval_cloud_input_price_per_million="0.15",
        eval_cloud_output_price_per_million="0.60",
    )

    profile = settings.eval_profile("cloud")
    provider = build_eval_llm_provider(settings, "cloud")

    assert profile.input_price_per_million == Decimal("0.15")
    assert profile.output_price_per_million == Decimal("0.60")
    assert provider.provider_name == "openai_compatible"
    assert provider.model_name == "example-model"


def test_cloud_eval_profile_rejects_missing_model() -> None:
    settings = Settings(
        _env_file=None,
        eval_cloud_base_url="https://llm.example.test/v1",
    )

    with pytest.raises(ValueError, match="Model is not configured"):
        build_eval_llm_provider(settings, "cloud")


def test_application_provider_factory_remains_compatible() -> None:
    settings = Settings(_env_file=None)

    provider = build_llm_provider(settings)

    assert provider.provider_name == "ollama"
    assert provider.model_name == "qwen2.5:7b-instruct"
