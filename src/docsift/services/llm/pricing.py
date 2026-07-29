from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from docsift.schemas.evals import EvalPricing

if TYPE_CHECKING:
    from docsift.core.config import Settings


class UnknownModelPricing(KeyError):
    """Модель отсутствует в таблице цен провайдеров."""


def calculate_cost_usd(
    pricing: EvalPricing,
    input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    """Стоимость вызова в USD.

    Локальные модели (цена 0) всегда дают ноль, даже если провайдер не вернул
    счётчики токенов. Для платной модели без известных токенов стоимость
    неизвестна — возвращаем None, чтобы eval-отчёт честно показал «неизвестна».
    """

    def component(tokens: int | None, price: Decimal) -> Decimal | None:
        if price == 0:
            return Decimal(0)
        if tokens is None:
            return None
        return Decimal(tokens) * price / Decimal(1_000_000)

    input_cost = component(input_tokens, pricing.input_price_per_million)
    output_cost = component(output_tokens, pricing.output_price_per_million)
    if input_cost is None or output_cost is None:
        return None
    return input_cost + output_cost


class PricingTable:
    """Реестр цен по моделям: цена за миллион токенов на вход и на выход.

    Ключ — идентификатор модели. Локальные модели регистрируются с нулевой
    ценой. Модели нет в таблице — стоимость неизвестна (``None``) либо
    явная ошибка через :meth:`require`.
    """

    def __init__(self, entries: Mapping[str, EvalPricing] | None = None) -> None:
        self._entries: dict[str, EvalPricing] = dict(entries or {})

    def register(self, model: str, pricing: EvalPricing) -> None:
        self._entries[model] = pricing

    def get(self, model: str) -> EvalPricing | None:
        return self._entries.get(model)

    def require(self, model: str) -> EvalPricing:
        pricing = self._entries.get(model)
        if pricing is None:
            raise UnknownModelPricing(model)
        return pricing

    def cost_usd(
        self,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> Decimal | None:
        pricing = self._entries.get(model)
        if pricing is None:
            return None
        return calculate_cost_usd(pricing, input_tokens, output_tokens)

    @property
    def models(self) -> list[str]:
        return list(self._entries)

    def items(self) -> list[tuple[str, EvalPricing]]:
        return list(self._entries.items())

    def __contains__(self, model: object) -> bool:
        return model in self._entries

    def __len__(self) -> int:
        return len(self._entries)


def build_pricing_table(settings: Settings) -> PricingTable:
    """Собрать таблицу цен из конфигурации профилей eval.

    Профиль ``local`` по умолчанию имеет нулевые цены, ``cloud`` —
    сконфигурированные. Каждая модель регистрации попадает в таблицу по своему
    идентификатору, что и даёт «цену по каждой модели».
    """
    table = PricingTable()
    for profile_name in ("local", "cloud"):
        profile = settings.eval_profile(profile_name)  # type: ignore[arg-type]
        if profile.model:
            table.register(
                profile.model,
                EvalPricing(
                    input_price_per_million=profile.input_price_per_million,
                    output_price_per_million=profile.output_price_per_million,
                ),
            )
    return table