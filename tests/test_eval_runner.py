from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from docsift.db.models import EvalRun
from docsift.domain.enums import EvalRunStatus
from docsift.schemas.evals import (
    DatasetManifest,
    EvalPricing,
    ExpectedDocument,
)
from docsift.schemas.llm import LLMRequest, LLMResponse
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.evals.dataset import Dataset, DatasetSample
from docsift.services.evals.runner import EvalRunner, calculate_cost_usd


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


def _dataset(tmp_path: Path, count: int) -> Dataset:
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
    def __init__(self, failing_names: set[str] | None = None) -> None:
        self.failing_names = failing_names or set()
        self.paths: list[Path] = []

    def extract(self, source_path: str | Path) -> TextExtractionResult:
        path = Path(source_path)
        self.paths.append(path)
        if path.name in self.failing_names:
            raise RuntimeError("text extraction failed")
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


def _runner(
    provider: FakeProvider,
    extractor: FakeTextExtractor,
    repository: FakeEvalRunRepository,
    pricing: EvalPricing,
    progress: list[tuple[int, int]],
    provider_profile: str = "local",
) -> EvalRunner:
    return EvalRunner(
        provider=provider,
        provider_profile=provider_profile,  # type: ignore[arg-type]
        pricing=pricing,
        prompt_version="v1",
        text_extractor=extractor,
        run_repository=repository,
        progress_callback=lambda completed, total: progress.append((completed, total)),
    )


@pytest.mark.asyncio
async def test_runner_applies_limit_reports_progress_and_local_cost(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), None, None)]
    )
    extractor = FakeTextExtractor()
    repository = FakeEvalRunRepository()
    progress: list[tuple[int, int]] = []
    pricing = EvalPricing(
        input_price_per_million=Decimal(0),
        output_price_per_million=Decimal(0),
    )

    report = await _runner(
        provider, extractor, repository, pricing, progress
    ).run(_dataset(tmp_path, 3), limit=1)

    assert report.sample_count == 1
    assert report.succeeded_count == 1
    assert report.failed_count == 0
    assert report.cost_usd == Decimal(0)
    assert report.metrics.fields["number"].matches == 1
    assert report.samples[0].duration_seconds >= 0
    assert report.provider == "local"
    assert report.provider_backend == "fake"
    assert report.model == "fake-model"
    assert report.prompt_version == "v1"
    assert report.temperature == 0
    assert report.average_duration_seconds == report.samples[0].duration_seconds
    assert progress == [(1, 1)]
    assert len(extractor.paths) == 1
    assert len(provider.requests) == 1
    assert repository.updated[-1].status is EvalRunStatus.COMPLETED
    json.dumps(repository.updated[-1].metrics)


@pytest.mark.asyncio
async def test_runner_keeps_going_after_one_document_fails(tmp_path: Path) -> None:
    provider = FakeProvider(
        [LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), 10, 5)]
    )
    extractor = FakeTextExtractor({"sample-1.pdf"})
    repository = FakeEvalRunRepository()
    progress: list[tuple[int, int]] = []
    pricing = EvalPricing(
        input_price_per_million=Decimal(0),
        output_price_per_million=Decimal(0),
    )

    report = await _runner(
        provider, extractor, repository, pricing, progress
    ).run(_dataset(tmp_path, 2))

    assert report.failed_count == 1
    assert report.succeeded_count == 1
    assert [sample.status for sample in report.samples] == ["failed", "succeeded"]
    assert report.samples[0].error_type == "RuntimeError"
    assert report.samples[0].error_message == "text extraction failed"
    assert report.samples[0].duration_seconds >= 0
    assert report.metrics.fields["number"].matches == 1
    assert progress == [(1, 2), (2, 2)]
    assert repository.updated[-1].status is EvalRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_runner_calculates_cloud_cost_from_configured_prices(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        [
            LLMResponse(
                json.dumps(_valid_payload(), ensure_ascii=False),
                input_tokens=100_000,
                output_tokens=50_000,
            )
        ]
    )
    repository = FakeEvalRunRepository()
    pricing = EvalPricing(
        input_price_per_million=Decimal(2),
        output_price_per_million=Decimal(4),
    )

    report = await _runner(
        provider,
        FakeTextExtractor(),
        repository,
        pricing,
        [],
        provider_profile="cloud",
    ).run(_dataset(tmp_path, 1))

    assert report.token_usage.input_tokens == 100_000
    assert report.token_usage.output_tokens == 50_000
    assert report.cost_usd == Decimal("0.4")
    assert report.samples[0].cost_usd == Decimal("0.4")
    assert report.provider == "cloud"


def test_cost_is_unknown_only_when_paid_token_usage_is_unknown() -> None:
    paid = EvalPricing(
        input_price_per_million=Decimal(1),
        output_price_per_million=Decimal(1),
    )
    free = EvalPricing(
        input_price_per_million=Decimal(0),
        output_price_per_million=Decimal(0),
    )

    assert calculate_cost_usd(paid, None, 10) is None
    assert calculate_cost_usd(free, None, None) == Decimal(0)


@pytest.mark.asyncio
async def test_runner_rejects_non_positive_limit(tmp_path: Path) -> None:
    provider = FakeProvider([])
    repository = FakeEvalRunRepository()
    pricing = EvalPricing(
        input_price_per_million=Decimal(0),
        output_price_per_million=Decimal(0),
    )

    with pytest.raises(ValueError, match="greater than zero"):
        await _runner(
            provider,
            FakeTextExtractor(),
            repository,
            pricing,
            [],
        ).run(_dataset(tmp_path, 1), limit=0)

    assert repository.created == []
