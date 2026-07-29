from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.llm import LLMRequest, LLMResponse
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.llm import (
    ExtractionCache,
    LLMExtractionService,
    LLMProviderError,
    content_hash,
)
from docsift.services.llm.cache import CacheKey


class FakeProvider:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(
        self,
        responses: list[LLMResponse | LLMProviderError],
        supports_json_schema: bool = True,
        model_name: str = "fake-model",
    ) -> None:
        self.responses = responses
        self.supports_json_schema = supports_json_schema
        self.model_name = model_name
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, LLMProviderError):
            raise response
        return response


class FakeExtractionRepository:
    def __init__(self) -> None:
        self.extractions: list[Any] = []

    async def next_attempt_no(self, document_id: Any) -> int:
        return 1 + sum(item.document_id == document_id for item in self.extractions)

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
        "text": "подтверждение",
    }


def _field(value: Any, confidence: float = 0.99) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": confidence, "sources": [_source()]}


def _valid_payload() -> dict[str, Any]:
    return {
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


def _valid_document() -> ExtractedDocument:
    return ExtractedDocument.model_validate(_valid_payload())


def _text_result(text: str = "Счёт №42") -> TextExtractionResult:
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
                        text=text,
                        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                        confidence=1,
                        source="pdf:text_layer",
                    )
                ],
            )
        ],
        used_ocr=False,
    )


def _service(
    provider: FakeProvider,
    cache: ExtractionCache,
    prompt_version: str = "v1",
) -> LLMExtractionService:
    return LLMExtractionService(
        provider,
        FakeExtractionRepository(),
        prompt_version=prompt_version,
        cache=cache,
    )


@pytest.mark.asyncio
async def test_cache_hit_skips_provider_call() -> None:
    """Попадание в кеш: повторный вызов не дёргает провайдера."""
    payload = json.dumps(_valid_payload(), ensure_ascii=False)
    provider = FakeProvider([LLMResponse(payload, 120, 80)])
    cache = ExtractionCache()
    service = _service(provider, cache)

    first = await service.extract(uuid4(), _text_result())
    second = await service.extract(uuid4(), _text_result())

    assert first.number.value == "42"
    assert second.number.value == "42"
    assert len(provider.requests) == 1  # провайдер вызван один раз
    assert cache.hits == 1
    assert cache.misses == 1


def test_cache_miss_when_prompt_version_changes() -> None:
    """Смена версии промта даёт другой ключ — старый результат невалиден (промах).

    Тестируем на уровне кеша: ключ обязан включать версию промта.
    Через сервис с v2 нельзя — файл промта v2 относится к этапу 2 (не наша задача).
    """
    cache = ExtractionCache()
    text = _text_result()
    key_v1 = cache.make_key(text, "v1", "fake-model")
    key_v2 = cache.make_key(text, "v2", "fake-model")

    assert str(key_v1) != str(key_v2)

    cache.store(key_v1, _valid_document())
    assert cache.get(key_v1) is not None  # попадание
    assert cache.get(key_v2) is None  # промах: версия промта изменилась


def test_cache_miss_when_model_changes() -> None:
    """Смена модели даёт другой ключ — старый результат невалиден (промах)."""
    cache = ExtractionCache()
    text = _text_result()
    key_a = cache.make_key(text, "v1", "model-a")
    key_b = cache.make_key(text, "v1", "model-b")

    assert str(key_a) != str(key_b)

    cache.store(key_a, _valid_document())
    assert cache.get(key_a) is not None
    assert cache.get(key_b) is None  # промах: модель изменилась


@pytest.mark.asyncio
async def test_bypass_cache_forces_provider_call() -> None:
    """bypass_cache=True — даже при наличии кеша провайдер вызывается всегда (для eval)."""
    payload = json.dumps(_valid_payload(), ensure_ascii=False)
    provider = FakeProvider([LLMResponse(payload, 10, 5), LLMResponse(payload, 20, 7)])
    cache = ExtractionCache()
    service = _service(provider, cache)

    await service.extract(uuid4(), _text_result())
    await service.extract(uuid4(), _text_result(), bypass_cache=True)

    assert len(provider.requests) == 2
    assert cache.hits == 0


@pytest.mark.asyncio
async def test_different_content_has_different_keys() -> None:
    """Разное содержимое → разные хеши → разные ключи → промах."""
    payload = json.dumps(_valid_payload(), ensure_ascii=False)
    provider = FakeProvider(
        [LLMResponse(payload, 10, 5), LLMResponse(payload, 20, 7)],
    )
    cache = ExtractionCache()
    service = _service(provider, cache)

    await service.extract(uuid4(), _text_result("Счёт №42"))
    await service.extract(uuid4(), _text_result("Счёт №99"))

    assert len(provider.requests) == 2
    assert cache.misses == 2


def test_content_hash_is_deterministic_and_path_independent() -> None:
    """Хеш зависит от содержимого, но не от source_path."""
    a = _text_result()
    b = TextExtractionResult(
        source_path="different/path.pdf",
        media_type=a.media_type,
        pages=a.pages,
        used_ocr=a.used_ocr,
    )
    assert content_hash(a) == content_hash(b)
    assert content_hash(_text_result("другой текст")) != content_hash(a)


def test_cache_key_string_includes_all_components() -> None:
    """Строковое представление ключа содержит версию схемы, промта и модель."""
    key = CacheKey(
        content_hash="abc123",
        prompt_version="v1",
        model="gpt-4o",
    )
    rendered = str(key)
    assert "v1" in rendered
    assert "gpt-4o" in rendered
    assert "abc123" in rendered