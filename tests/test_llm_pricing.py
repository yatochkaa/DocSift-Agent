from __future__ import annotations

from decimal import Decimal

import pytest

from docsift.schemas.evals import EvalPricing
from docsift.services.llm import (
    PricingTable,
    UnknownModelPricing,
    build_pricing_table,
    calculate_cost_usd,
)


def _pricing(input_price: int | str = 0, output_price: int | str = 0) -> EvalPricing:
    return EvalPricing(
        input_price_per_million=Decimal(input_price),
        output_price_per_million=Decimal(output_price),
    )


def test_calculate_cost_usd_for_cloud_model() -> None:
    """Цена облачной модели: 100k вход * $2/M + 50k выход * $4/M = $0.4."""
    pricing = _pricing(input_price=2, output_price=4)
    cost = calculate_cost_usd(pricing, input_tokens=100_000, output_tokens=50_000)
    assert cost == Decimal("0.4")


def test_calculate_cost_usd_for_local_model_is_zero_even_without_tokens() -> None:
    """Локальная модель: цена 0 → стоимость всегда 0, даже если токены неизвестны."""
    pricing = _pricing(input_price=0, output_price=0)
    assert calculate_cost_usd(pricing, input_tokens=None, output_tokens=None) == Decimal(0)
    assert calculate_cost_usd(pricing, input_tokens=999_999, output_tokens=999_999) == Decimal(0)


def test_calculate_cost_usd_unknown_when_paid_tokens_missing() -> None:
    """Платная модель без счётчиков токенов → стоимость неизвестна."""
    pricing = _pricing(input_price=1, output_price=1)
    assert calculate_cost_usd(pricing, input_tokens=None, output_tokens=10) is None
    assert calculate_cost_usd(pricing, input_tokens=10, output_tokens=None) is None


def test_pricing_table_returns_cost_by_model() -> None:
    table = PricingTable(
        {
            "qwen2.5-coder:7b": _pricing(0, 0),
            "gpt-4o": _pricing(2.5, 10),
        }
    )
    assert table.cost_usd("gpt-4o", 1_000_000, 1_000_000) == Decimal("12.5")
    assert table.cost_usd("qwen2.5-coder:7b", None, None) == Decimal(0)


def test_pricing_table_missing_model_is_unknown_or_explicit_error() -> None:
    """Отсутствие модели в таблице цен: cost_usd → None, require → KeyError."""
    table = PricingTable({"gpt-4o": _pricing(2.5, 10)})
    assert table.cost_usd("unknown-model", 100, 100) is None
    assert table.get("unknown-model") is None
    with pytest.raises(UnknownModelPricing):
        table.require("unknown-model")


def test_build_pricing_table_from_settings() -> None:
    """Конфигурация: local-профиль даёт нулевую цену, cloud — сконфигурированную."""
    from docsift.core.config import Settings

    settings = Settings(
        eval_local_model="qwen2.5-coder:7b",
        eval_cloud_model="gpt-4o-mini",
        eval_cloud_input_price_per_million=Decimal("0.15"),
        eval_cloud_output_price_per_million=Decimal("0.6"),
    )
    table = build_pricing_table(settings)
    assert "qwen2.5-coder:7b" in table
    assert "gpt-4o-mini" in table
    assert table.require("qwen2.5-coder:7b").input_price_per_million == Decimal(0)
    assert table.require("gpt-4o-mini").output_price_per_million == Decimal("0.6")