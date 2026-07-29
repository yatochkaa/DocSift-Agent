from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from docsift.schemas.guardrails import GuardrailResult, GuardrailViolation
from docsift.schemas.llm import LLMRequest, LLMResponse
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.llm.cascade import CascadeExtractionService
from docsift.services.llm.providers import LLMProviderError


class FakeProvider:
    def __init__(
        self,
        responses: list[LLMResponse | LLMProviderError],
        model_name: str = "fake-model",
        provider_name: str = "fake",
        supports_json_schema: bool = True,
    ) -> None:
        self._responses = responses
        self.model_name = model_name
        self.provider_name = provider_name
        self.supports_json_schema = supports_json_schema
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = self._responses[len(self.requests) - 1]
        if isinstance(response, LLMProviderError):
            raise response
        return response


class FakeRepository:
    def __init__(self) -> None:
        self.extractions: list[Any] = []

    async def next_attempt_no(self, document_id: Any) -> int:
        return 1

    async def create(self, extraction: Any) -> Any:
        self.extractions.append(extraction)
        return extraction

    async def update(self, extraction: Any) -> Any:
        return extraction


def _source() -> dict[str, Any]:
    return {
        "kind": "pdf_text",
        "page": 1,
        "bbox": None,
        "sheet": None,
        "cell_range": None,
        "text": "source",
    }


def _field(value: Any, confidence: float = 0.99) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": confidence, "sources": [_source()]}


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "document_type": _field("payment_invoice"),
        "number": _field("42"),
        "date": _field(date(2026, 1, 10).isoformat()),
        "supplier": {
            "name": _field("ООО Поставщик"),
            "inn": _field("7707083893"),
            "kpp": _field("773601001"),
        },
        "buyer": {
            "name": _field("ИП Покупатель"),
            "inn": _field("500100732259"),
            "kpp": _field(None),
        },
        "total_amount": _field("1200.00"),
        "vat_amount": _field("200.00"),
        "currency": _field("RUB"),
        "line_items": [],
    }
    payload.update(overrides)
    return payload


def _text_result() -> TextExtractionResult:
    return TextExtractionResult(
        source_path="doc.pdf",
        media_type="application/pdf",
        pages=[
            ExtractedPage(
                number=1,
                width=100,
                height=100,
                blocks=[
                    TextBlock(
                        text="Счёт №42",
                        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                        confidence=1,
                        source="pdf:text_layer",
                    )
                ],
            )
        ],
        used_ocr=False,
    )


def _clean_guardrail(document: Any) -> GuardrailResult:
    return GuardrailResult(requires_review=False, violations=[])


def _inn_violation_guardrail(document: Any) -> GuardrailResult:
    return GuardrailResult(
        requires_review=True,
        violations=[
            GuardrailViolation(
                rule="invalid_inn",
                field_path="supplier/inn",
                message="ИНН не прошёл проверку контрольной суммы",
                actual="123",
            )
        ],
    )


@pytest.mark.asyncio
async def test_cascade_accepts_clean_cheap_result_without_second_call() -> None:
    """Чистый результат дешёвой модели — дорогая не вызывается."""
    cheap_payload = json.dumps(_valid_payload(), ensure_ascii=False)
    cheap_provider = FakeProvider([LLMResponse(cheap_payload, 100, 50)], model_name="cheap-model")
    expensive_provider = FakeProvider([], model_name="expensive-model")

    service = CascadeExtractionService(
        cheap_provider,
        expensive_provider,
        FakeRepository(),
        guardrail_evaluator=_clean_guardrail,
    )

    result = await service.extract(uuid4(), _text_result())

    assert result.used_expensive_model is False
    assert len(expensive_provider.requests) == 0
    assert result.document.number.value == "42"
    assert result.provenance.cheap_model == "cheap-model"
    assert result.provenance.expensive_model == "expensive-model"
    assert "supplier/inn" in result.provenance.cheap_fields
    assert result.provenance.source_of("supplier/inn") == "cheap-model"
    assert len(result.provenance.expensive_fields) == 0


@pytest.mark.asyncio
async def test_cascade_re_asks_expensive_only_for_disputed_fields() -> None:
    """Нарушение guardrails → дорогая модель переспрашивает только спорные поля."""
    cheap_payload = json.dumps(_valid_payload(), ensure_ascii=False)
    cheap_provider = FakeProvider([LLMResponse(cheap_payload, 100, 50)], model_name="cheap-model")

    correction = json.dumps(
        {"supplier/inn": _field("7707083893", 0.95)},
        ensure_ascii=False,
    )
    expensive_provider = FakeProvider(
        [LLMResponse(correction, 200, 60)],
        model_name="expensive-model",
    )

    service = CascadeExtractionService(
        cheap_provider,
        expensive_provider,
        FakeRepository(),
        guardrail_evaluator=_inn_violation_guardrail,
    )

    result = await service.extract(uuid4(), _text_result())

    assert result.used_expensive_model is True
    assert len(expensive_provider.requests) == 1
    # В запросе к дорогой модели упомянут только спорный путь, а не весь документ
    expensive_request = expensive_provider.requests[0]
    disputed_content = expensive_request.messages[-1].content
    # Спорные поля переданы
    assert "supplier/inn" in disputed_content
    # Поле number/value не переспрашивается — оно не в списке спорных
    assert '"number"' not in disputed_content.split("Спорные поля")[1]
    # Документ обновлён — ИНН исправлен
    assert result.document.supplier.inn.value == "7707083893"


@pytest.mark.asyncio
async def test_cascade_provenance_shows_which_model_gave_which_field() -> None:
    """Провенанс: видно, какое поле от какой модели."""
    cheap_payload = json.dumps(_valid_payload(), ensure_ascii=False)
    cheap_provider = FakeProvider([LLMResponse(cheap_payload, 100, 50)], model_name="cheap-model")
    correction = json.dumps(
        {"supplier/inn": _field("7707083893", 0.95)},
        ensure_ascii=False,
    )
    expensive_provider = FakeProvider(
        [LLMResponse(correction, 200, 60)],
        model_name="expensive-model",
    )

    service = CascadeExtractionService(
        cheap_provider,
        expensive_provider,
        FakeRepository(),
        guardrail_evaluator=_inn_violation_guardrail,
    )

    result = await service.extract(uuid4(), _text_result())

    provenance = result.provenance
    # supplier/inn пришёл от дорогой модели
    assert provenance.source_of("supplier/inn") == "expensive-model"
    # number остался от дешёвой
    assert provenance.source_of("number") == "cheap-model"
    # buyer/name тоже от дешёвой
    assert provenance.source_of("buyer/name") == "cheap-model"
    assert "supplier/inn" in provenance.re_asked_paths


async def test_correction_for_non_disputed_path_is_ignored() -> None:
    """Правка по пути, которого нет в disputed_paths, отбрасывается."""
    cheap_payload = json.dumps(_valid_payload(), ensure_ascii=False)
    cheap_provider = FakeProvider([LLMResponse(cheap_payload, 100, 50)], model_name="cheap-model")

    # Дорогая модель возвращает два ключа: оспоренный и неоспоренный
    correction = json.dumps(
        {
            "supplier/inn": _field("7707083893", 0.95),
            "number": _field("999", 0.99),
        },
        ensure_ascii=False,
    )
    expensive_provider = FakeProvider(
        [LLMResponse(correction, 200, 60)],
        model_name="expensive-model",
    )

    service = CascadeExtractionService(
        cheap_provider,
        expensive_provider,
        FakeRepository(),
        guardrail_evaluator=_inn_violation_guardrail,
    )

    result = await service.extract(uuid4(), _text_result())

    # supplier/inn — оспоренное поле — исправлено
    assert result.document.supplier.inn.value == "7707083893"
    # number — неоспоренное поле — осталось прежним
    assert result.document.number.value == "42"


async def test_correction_for_disputed_path_is_applied() -> None:
    """Правка по оспоренному пути применяется — фильтр не ломает основной сценарий."""
    cheap_payload = json.dumps(_valid_payload(), ensure_ascii=False)
    cheap_provider = FakeProvider([LLMResponse(cheap_payload, 100, 50)], model_name="cheap-model")
    correction = json.dumps({"supplier/inn": _field("7743013902", 0.95)}, ensure_ascii=False)
    expensive_provider = FakeProvider([LLMResponse(correction, 200, 60)], model_name="expensive-model")
    service = CascadeExtractionService(cheap_provider, expensive_provider, FakeRepository(),
                                       guardrail_evaluator=_inn_violation_guardrail)
    result = await service.extract(uuid4(), _text_result())
    assert result.used_expensive_model is True
    assert result.document.supplier.inn.value == "7743013902"
    assert float(result.document.supplier.inn.confidence) == 0.95


async def test_dropped_correction_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Отброшенная правка попадает в лог как предупреждение."""
    import logging

    caplog.set_level(logging.WARNING, logger="docsift.services.llm.cascade")

    cheap_payload = json.dumps(_valid_payload(), ensure_ascii=False)
    cheap_provider = FakeProvider([LLMResponse(cheap_payload, 100, 50)], model_name="cheap-model")

    # Дорогая модель возвращает только неоспоренный путь
    correction = json.dumps(
        {"number": _field("999", 0.99)},
        ensure_ascii=False,
    )
    expensive_provider = FakeProvider(
        [LLMResponse(correction, 200, 60)],
        model_name="expensive-model",
    )

    service = CascadeExtractionService(
        cheap_provider,
        expensive_provider,
        FakeRepository(),
        guardrail_evaluator=_inn_violation_guardrail,
    )

    result = await service.extract(uuid4(), _text_result())

    # Значение не изменилось — правка отброшена
    assert result.document.number.value == "42"
    # В логе есть запись об отброшенных правках
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert "number" in record.message