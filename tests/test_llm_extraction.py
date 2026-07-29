from __future__ import annotations

import copy
import json
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from PIL import Image

from docsift.db.models import Extraction
from docsift.domain.enums import ExtractionStatus
from docsift.schemas.llm import LLMRequest, LLMResponse
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.llm import (
    LLMExtractionError,
    LLMExtractionService,
    LLMProviderError,
)


class FakeProvider:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(
        self,
        responses: list[LLMResponse | LLMProviderError],
        supports_json_schema: bool = True,
    ) -> None:
        self.responses = responses
        self.supports_json_schema = supports_json_schema
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, LLMProviderError):
            raise response
        return response


class FakeExtractionRepository:
    def __init__(self) -> None:
        self.extractions: list[Extraction] = []
        self.update_count = 0

    async def next_attempt_no(self, document_id: UUID) -> int:
        return 1 + sum(item.document_id == document_id for item in self.extractions)

    async def create(self, extraction: Extraction) -> Extraction:
        self.extractions.append(extraction)
        return extraction

    async def update(self, extraction: Extraction) -> Extraction:
        self.update_count += 1
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


@pytest.fixture
def text_result(tmp_path) -> TextExtractionResult:
    image_path = tmp_path / "unused.png"
    Image.new("RGB", (10, 10), "white").save(image_path)
    return TextExtractionResult(
        source_path=str(image_path),
        media_type="application/pdf",
        pages=[
            ExtractedPage(
                number=1,
                width=100,
                height=100,
                blocks=[
                    TextBlock(
                        text="Счёт №42, ИНН 7707083893, итого 1200",
                        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                        confidence=1,
                        source="pdf:text_layer",
                    )
                ],
            )
        ],
        used_ocr=False,
    )


@pytest.mark.asyncio
async def test_valid_response_is_validated_and_audited(text_result: TextExtractionResult) -> None:
    provider = FakeProvider(
        [LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), 120, 80)],
        supports_json_schema=False,
    )
    repository = FakeExtractionRepository()
    service = LLMExtractionService(provider, repository)

    result = await service.extract(uuid4(), text_result)

    assert result.number.value == "42"
    assert len(provider.requests) == 1
    assert "Провайдер не поддерживает" in provider.requests[0].messages[0].content
    extraction = repository.extractions[0]
    assert extraction.status is ExtractionStatus.SUCCEEDED
    assert extraction.prompt_version == "v1"
    assert extraction.raw_response is not None
    assert extraction.input_tokens == 120
    assert extraction.output_tokens == 80
    assert extraction.response_time_ms is not None
    assert extraction.result is not None


@pytest.mark.asyncio
async def test_broken_json_fails_cleanly_after_two_attempts(
    text_result: TextExtractionResult,
) -> None:
    provider = FakeProvider([LLMResponse("{broken"), LLMResponse("not-json")])
    repository = FakeExtractionRepository()

    with pytest.raises(LLMExtractionError):
        await LLMExtractionService(provider, repository).extract(uuid4(), text_result)

    assert len(provider.requests) == 2
    assert repository.extractions[0].status is ExtractionStatus.FAILED


@pytest.mark.asyncio
async def test_schema_error_is_sent_back_and_retry_succeeds(
    text_result: TextExtractionResult,
) -> None:
    invalid = _valid_payload()
    invalid["supplier"]["inn"] = _field("12345")
    provider = FakeProvider(
        [
            LLMResponse(json.dumps(invalid, ensure_ascii=False)),
            LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False)),
        ]
    )
    repository = FakeExtractionRepository()

    result = await LLMExtractionService(provider, repository).extract(uuid4(), text_result)

    assert result.supplier.inn.value == "7707083893"
    assert len(provider.requests) == 2
    feedback = provider.requests[1].messages[-1].content
    assert "Pydantic" in feedback
    assert "supplier.inn" in feedback
    assert len(repository.extractions[0].llm_attempts) == 2


@pytest.mark.asyncio
async def test_two_invalid_attempts_preserve_both_raw_responses(
    text_result: TextExtractionResult,
) -> None:
    first = _valid_payload()
    first["total_amount"] = _field("-1.00")
    second = copy.deepcopy(first)
    second["document_type"] = _field("unknown_document")
    provider = FakeProvider(
        [
            LLMResponse(json.dumps(first, ensure_ascii=False)),
            LLMResponse(json.dumps(second, ensure_ascii=False)),
        ]
    )
    repository = FakeExtractionRepository()

    with pytest.raises(LLMExtractionError) as error:
        await LLMExtractionService(provider, repository).extract(uuid4(), text_result)

    extraction = repository.extractions[0]
    assert len(error.value.validation_errors) == 2
    assert len(extraction.llm_attempts) == 2
    assert extraction.raw_response == json.dumps(second, ensure_ascii=False)
    assert extraction.error_code == "schema_validation_failed"


@pytest.mark.asyncio
async def test_provider_error_is_audited_without_retry(
    text_result: TextExtractionResult,
) -> None:
    provider = FakeProvider([LLMProviderError("Ollama request failed")])
    repository = FakeExtractionRepository()

    with pytest.raises(LLMProviderError, match="Ollama request failed"):
        await LLMExtractionService(provider, repository).extract(uuid4(), text_result)

    extraction = repository.extractions[0]
    assert len(provider.requests) == 1
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.error_code == "provider_error"
    assert extraction.error_message == "Ollama request failed"
    assert extraction.llm_attempts == [
        {"attempt": 1, "provider_error": "Ollama request failed"}
    ]
    assert extraction.response_time_ms is not None
    assert repository.update_count == 1
