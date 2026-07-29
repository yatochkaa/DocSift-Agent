from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from docsift.db.models import EvalRun
from docsift.schemas.evals import DatasetManifest, EvalPricing, ExpectedDocument
from docsift.schemas.llm import LLMRequest, LLMResponse
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.evals.dataset import Dataset, DatasetSample
from docsift.services.evals.runner import EvalRunner


def _source() -> dict[str, Any]:
    return {
        "kind": "pdf_text",
        "page": 1,
        "bbox": None,
        "sheet": None,
        "cell_range": None,
        "text": "source",
    }


def _field(value: Any) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": 0.99, "sources": [_source()]}


def _valid_payload() -> dict[str, Any]:
    return {
        "document_type": _field("payment_invoice"),
        "number": _field("42"),
        "date": _field("2026-01-10"),
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


def _dataset(tmp_path: Path, count: int = 1) -> Dataset:
    expected = ExpectedDocument(number="42")
    samples = tuple(
        DatasetSample(
            sample_id=f"sample-{index}",
            document_path=tmp_path / f"sample-{index}.pdf",
            expected_path=tmp_path / f"sample-{index}.expected.json",
            expected=expected,
        )
        for index in range(1, count + 1)
    )
    return Dataset(
        root=tmp_path,
        manifest=DatasetManifest(name="accounting", version="v1"),
        samples=samples,
    )


class FakeProvider:
    provider_name = "fake"
    model_name = "fake-model"
    supports_json_schema = True

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._responses[len(self.requests) - 1]


class FakeTextExtractor:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def extract(self, source_path: str | Path) -> TextExtractionResult:
        path = Path(source_path)
        self.paths.append(path)
        return TextExtractionResult(
            source_path=str(path),
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


class FakeEvalRunRepository:
    def __init__(self) -> None:
        self.created: list[EvalRun] = []
        self.updated: list[EvalRun] = []

    async def create(self, eval_run: EvalRun) -> EvalRun:
        self.created.append(eval_run)
        return eval_run

    async def update(self, eval_run: EvalRun) -> EvalRun:
        self.updated.append(eval_run)
        return eval_run


def _free_pricing() -> EvalPricing:
    return EvalPricing(
        input_price_per_million=Decimal(0),
        output_price_per_million=Decimal(0),
    )


def _runner(
    provider: FakeProvider,
    extractor: FakeTextExtractor,
    repository: FakeEvalRunRepository,
    *,
    bypass_cache: bool = True,
) -> EvalRunner:
    return EvalRunner(
        provider=provider,
        provider_profile="local",
        pricing=_free_pricing(),
        prompt_version="v1",
        text_extractor=extractor,
        run_repository=repository,
        progress_callback=None,
        bypass_cache=bypass_cache,
    )


@pytest.mark.asyncio
async def test_runner_default_bypass_cache_is_true(tmp_path: Path) -> None:
    """Раннер по умолчанию вызывает сервис с bypass_cache=True."""
    payload = json.dumps(_valid_payload(), ensure_ascii=False)
    provider = FakeProvider([LLMResponse(payload, 10, 5)])
    repository = FakeEvalRunRepository()

    runner = _runner(provider, FakeTextExtractor(), repository)
    assert runner._bypass_cache is True

    await runner.run(_dataset(tmp_path))

    run_config = repository.created[0].run_config
    assert run_config["bypass_cache"] is True
    assert run_config["strategy"] == "cheap_only"
    # Провайдер вызван — кеш не перехватил
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_runner_with_use_cache_passes_bypass_false(tmp_path: Path) -> None:
    """С флагом use_cache=False (bypass_cache=False) раннер передаёт это в сервис."""
    payload = json.dumps(_valid_payload(), ensure_ascii=False)
    provider = FakeProvider([LLMResponse(payload, 10, 5)])
    repository = FakeEvalRunRepository()

    runner = _runner(
        provider, FakeTextExtractor(), repository, bypass_cache=False
    )
    assert runner._bypass_cache is False

    await runner.run(_dataset(tmp_path))

    run_config = repository.created[0].run_config
    assert run_config["bypass_cache"] is False